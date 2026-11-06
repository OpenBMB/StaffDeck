"""Public display facts from the admitted composition, shared by all channel renderers."""


def composition_trace_names(staff):
    skills, steps, tools = {}, {}, {}
    for sop in staff.sops:
        skills[sop.skill_id] = sop.name
        steps[sop.skill_id] = {str(node['node_id']): str(node.get('name') or node['node_id'])
                              for node in sop.content.get('nodes', []) if isinstance(node, dict) and node.get('node_id')}
    for item in (*staff.capabilities, *(cap for sop in staff.sops for cap in sop.capabilities)):
        display = str(item.metadata.get('display_name') or item.name)
        name = ('general_skill.' + str(item.metadata.get('slug') or item.name)
                if item.resource_type == 'general_skill' else item.name)
        tools[name] = display
        tools[item.resource_id] = display
    return {'skills': skills, 'steps': steps, 'tools': tools}


def merge_trace_names(payload, skills, steps, tools):
    names = payload.get('trace_names')
    if not isinstance(names, dict):
        return
    for key, dest in (('skills', skills), ('tools', tools)):
        if isinstance(names.get(key), dict):
            dest.update({k: v for k, v in names[key].items() if isinstance(k, str) and isinstance(v, str)})
    if isinstance(names.get('steps'), dict):
        for key, values in names['steps'].items():
            if isinstance(key, str) and isinstance(values, dict):
                steps.setdefault(key, {}).update({k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)})
