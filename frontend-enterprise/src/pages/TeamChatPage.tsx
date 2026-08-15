import { Navigate, useParams } from 'react-router-dom';

import { EnterpriseRoute } from '../enums/routes';

/** Keep old team-chat links, but resolve them inside the owning team workspace. */
export default function TeamChatPage() {
  const { teamId = '' } = useParams<{ teamId: string }>();
  if (!teamId) return <Navigate to={EnterpriseRoute.Teams} replace />;
  return <Navigate to={`${EnterpriseRoute.Teams}/${teamId}?view=chat`} replace />;
}
