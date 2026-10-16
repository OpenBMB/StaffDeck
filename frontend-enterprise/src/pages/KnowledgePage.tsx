import KnowledgeFrame from '../business-ui/KnowledgeFrame';
import type { EnterpriseAuthUser } from '../auth';
import { useNavigate } from 'react-router-dom';
import { getEnterpriseAuthSession } from '../auth';
import { toast } from 'sonner';

type Props = { currentUser?: EnterpriseAuthUser; onLogout?: () => void };
export default function KnowledgeManagePage({ currentUser, onLogout }: Props = {}) {
  const navigate = useNavigate();
  return <KnowledgeFrame src="/km-ui/ui/v1/index.html" apiPath="/api/km/v1" headers={authHeaders} directory={directory} navigate={navigate} notify={notify} />;
}

export function KnowledgeAddPage({ currentUser }: Props = {}) {
  return <KnowledgeManagePage currentUser={currentUser} />;
}

function authHeaders(): Record<string, string> {
  const token = getEnterpriseAuthSession()?.token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function notify(message: string, error: boolean) {
  if (error) toast.error(message);
  else toast(message);
}

async function directory() {
  const response = await fetch('/api/km/v1/spaces', { headers: authHeaders() });
  if (!response.ok) throw new Error('Knowledge service unavailable');
  const spaces = await response.json();
  const models = (await Promise.all(spaces.map(async (space: { id: string }) =>
    (await loadModelOptions(space.id)).map(model => ({ ...model, space_id: space.id }))
  ))).flat();
  const scope = localStorage.getItem('ultrarag_enterprise_agent_scope');
  const initialSpaceId = spaces.find((space: { id: string }) => scope && space.id.endsWith(':' + scope))?.id;
  return { spaces, models, publishSuggestions: true, initialSpaceId };
}

async function loadModelOptions(spaceId: string): Promise<Array<{ id: string; name: string }>> {
  const tenant = spaceId.split(':')[1];
  const response = await fetch('/api/enterprise/model-configs?tenant_id=' + encodeURIComponent(tenant), { headers: authHeaders() });
  if (!response.ok) throw new Error('Model options unavailable');
  return (await response.json()).filter((row: { enabled: boolean }) => row.enabled);
}
