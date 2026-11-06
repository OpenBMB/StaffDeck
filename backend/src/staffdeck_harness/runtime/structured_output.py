"""Runtime-owned structured output validation and bounded model correction.

Domain modules supply schemas/validators and fallback decisions. Transports
supply model calls. This policy never executes or replays a business operation.
"""
import json
import re
from copy import deepcopy


class StructuredOutputError(ValueError):
    pass


def parse_json_object(text):
    text = (text or '').strip()
    if not text:
        raise ValueError('empty model output')
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
    candidate = match.group(1) if match else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end+1])
    if not isinstance(value, dict):
        raise ValueError('model output is not a JSON object')
    return value


def generate_structured(call, payload, validate, *, phase, max_repairs=1, trace=None):
    """Only validation failures are repairable; transport/auth/cancellation propagate."""
    current = deepcopy(payload)
    for attempt in range(max_repairs + 1):
        raw = call(current, attempt)
        try:
            return validate(parse_json_object(raw))
        except ValueError as exc:
            if attempt >= max_repairs:
                raise StructuredOutputError(f'{phase} structured output repair exhausted: {exc}') from exc
            current = deepcopy(payload)
            current['_schema_repair'] = {
                'attempt': attempt+1, 'max_attempts': max_repairs,
                'previous_output': str(raw)[:2000], 'validation_errors': [str(exc)[:1500]],
                'instruction': '仅修正结构化输出。所有字符串必须使用 JSON 双引号，不能使用裸标识符或 Python 字面量；遵守 schema，不重复已执行的业务操作。',
            }
            if trace:
                trace('runtime_structured_repair', {'phase':phase, 'attempt':attempt+1, 'max_attempts':max_repairs})


class SubmissionRecovery:
    """SOP result protocol failures are separate from business capability recovery."""
    def __init__(self, max_failures=3):
        self.max_failures = max_failures
        self.failures = 0
        self.pending = None

    def observe(self, result, *, schema, allowed_next_steps):
        if result.success:
            self.pending = None
            return None
        if (result.error or {}).get('code') != 'INVALID_ARGUMENTS':
            self.pending = None
            return None
        self.failures += 1
        self.pending = {'phase':'sop_result', 'schema':schema,
            'allowed_next_steps': sorted(allowed_next_steps), 'error':dict(result.error),
            'instruction':'只修正步骤结果，尤其 next_step_id 必须是带双引号的字符串或 null。保留已获得的知识和工具结果，不要重新调用业务能力。'}
        if self.failures >= self.max_failures:
            return {'code':'SOP_RESULT_REPAIR_EXHAUSTED', 'message':'SOP 步骤结果格式校验未通过，执行成果已保留，本次尚未提交完成状态。请重试结果提交，无需重复业务操作。',
                'details':{'phase':'sop_result','cause_code':'INVALID_ARGUMENTS','attempts':self.failures}}
        return None


def validate_schema(value, schema):
    from jsonschema import validate, ValidationError
    try:
        validate(value, schema)
    except ValidationError as exc:
        raise ValueError(exc.message) from exc
    return value


def repair_instruction(pending):
    if pending.get('phase') == 'sop_result':
        instruction = '当前 SOP 业务执行结果已保留，只修正步骤完成 JSON。next_step_id 必须使用带双引号的字符串或 null；不得重新调用已成功的知识检索或业务工具。'
    else:
        instruction = '上一条工具请求因参数 JSON/schema 无效而在执行前被拒绝。请根据工具 schema 修正且仅重新提交这条未执行的请求；不要重复任何已受理或已成功的业务动作。'
    return instruction + '\n' + json.dumps(pending, ensure_ascii=False)


def validate_result(value, schema):
    from staffdeck_harness.contracts.invocation import ModuleResult
    try:
        return ModuleResult.ok(validate_schema(value, schema))
    except ValueError as exc:
        return ModuleResult.fail('INVALID_ARGUMENTS',
            '结果必须是完整 JSON 对象，字符串（包括 next_step_id）必须加双引号。'+str(exc))


def repair_candidate(result, receipt, *, tool, current=None, context=None):
    if (result.error or {}).get('code') == 'INVALID_ARGUMENTS' and receipt is None:
        return {'tool':tool, 'error':dict(result.error), **(context or {})}
    return None if current and current.get('tool') == tool else current
