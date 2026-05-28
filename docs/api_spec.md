# API Spec

Base URL: `http://localhost:8000`

## Chat

- `POST /api/chat/turn`
- `POST /api/chat/sessions`
- `GET /api/chat/sessions?tenant_id=&user_id=`
- `GET /api/chat/sessions/{session_id}/messages?tenant_id=`
- `POST /api/chat/messages/{message_id}/feedback`
- `DELETE /api/chat/messages/{message_id}/feedback?tenant_id=`

## Enterprise Skills

- `GET /api/enterprise/skills?tenant_id=`
- `POST /api/enterprise/skills`
- `GET /api/enterprise/skills/{skill_id}?tenant_id=`
- `PUT /api/enterprise/skills/{skill_id}`
- `POST /api/enterprise/skills/{skill_id}/publish?tenant_id=`
- `POST /api/enterprise/skills/{skill_id}/archive?tenant_id=`
- `POST /api/enterprise/skills/distill`

## Enterprise Model Configs

- `GET /api/enterprise/model-configs?tenant_id=`
- `POST /api/enterprise/model-configs`
- `PUT /api/enterprise/model-configs/{id}`
- `POST /api/enterprise/model-configs/{id}/set-default?tenant_id=`
- `POST /api/enterprise/model-configs/{id}/test?tenant_id=`

## Enterprise Tools

- `GET /api/enterprise/tools?tenant_id=`
- `POST /api/enterprise/tools`
- `GET /api/enterprise/tools/{tool_id}?tenant_id=`
- `PUT /api/enterprise/tools/{tool_id}`
- `POST /api/enterprise/tools/{tool_id}/test`

## Sessions And Traces

- `GET /api/enterprise/sessions?tenant_id=`
- `GET /api/enterprise/sessions/{session_id}?tenant_id=`
- `POST /api/enterprise/sessions/{session_id}/reset?tenant_id=`
- `GET /api/enterprise/feedback/sessions?tenant_id=&rating=down`
- `GET /api/enterprise/feedback/sessions/{session_id}?tenant_id=`
- `GET /api/enterprise/traces?tenant_id=`
- `GET /api/enterprise/traces/{session_id}?tenant_id=`

## Mock

- `POST /api/mock/order/query`
