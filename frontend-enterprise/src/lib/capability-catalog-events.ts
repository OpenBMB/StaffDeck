export const ENTERPRISE_CAPABILITY_CATALOG_CHANGED_EVENT =
  'ultrarag-enterprise-capability-catalog-changed';

export type EnterpriseCapabilityResourceType = 'tool' | 'skill' | 'knowledge' | 'sop';

export type EnterpriseCapabilityCatalogChange = {
  resourceType: EnterpriseCapabilityResourceType;
  agentId?: string;
};

export function announceEnterpriseCapabilityCatalogChange(
  detail: EnterpriseCapabilityCatalogChange,
) {
  window.dispatchEvent(
    new CustomEvent<EnterpriseCapabilityCatalogChange>(
      ENTERPRISE_CAPABILITY_CATALOG_CHANGED_EVENT,
      { detail },
    ),
  );
}

export function subscribeEnterpriseCapabilityCatalogRefresh(listener: () => void) {
  const onFocus = () => listener();
  const onPageShow = () => listener();
  const onVisibilityChange = () => {
    if (document.visibilityState === 'visible') listener();
  };
  const onCatalogChange = () => listener();

  window.addEventListener('focus', onFocus);
  window.addEventListener('pageshow', onPageShow);
  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener(ENTERPRISE_CAPABILITY_CATALOG_CHANGED_EVENT, onCatalogChange);

  return () => {
    window.removeEventListener('focus', onFocus);
    window.removeEventListener('pageshow', onPageShow);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener(ENTERPRISE_CAPABILITY_CATALOG_CHANGED_EVENT, onCatalogChange);
  };
}
