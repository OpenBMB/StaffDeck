from types import SimpleNamespace as NS
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.hooks import HookDecision
from staffdeck_harness.runtime.result_policy import project_result
from app.core.turn_coordinator import _globalize_citations


def test_sync_and_async_common_projection_normalizes_before_hooks_and_caching():
    original = ModuleResult.ok({},citations=({'label':'[1]','source_path':'doc','excerpt':'source text'},))
    observed = []
    def hook(point, invocation, result):
        observed.extend(result.citations)
        return HookDecision.passthrough()
    result = project_result(hook,None,original)
    assert result.citations[0]['id'] == observed[0]['id']
    assert project_result(None,None,original).citations == result.citations
    assert 'id' not in original.citations[0]
    assert not project_result(lambda *a: HookDecision.deny('blocked'),None,original).citations


def test_hook_replacement_citations_are_also_normalized():
    replacement = ModuleResult.ok({},citations=({'source_path':'other','excerpt':'text'},))
    result = project_result(lambda *a: HookDecision(kind='modify',replacement=replacement),None,ModuleResult.ok({}))
    assert result.citations[0]['id'].startswith('kref_')


def test_global_numbering_deduplicates_sources_not_document_fragments():
    first = NS(citations=[{'source_path':'doc','excerpt':'A','label':'[1]'},
                          {'source_path':'doc','excerpt':'B','label':'[2]'}])
    second = NS(citations=[{'source_path':'doc','excerpt':'A','label':'[1]'}])
    result = _globalize_citations([first,second])
    assert len(result) == 2 and len({row['id'] for row in result}) == 2
    assert second.citations[0]['id'] == first.citations[0]['id']
    assert [row['label'] for row in result] == ['[1]', '[2]']
