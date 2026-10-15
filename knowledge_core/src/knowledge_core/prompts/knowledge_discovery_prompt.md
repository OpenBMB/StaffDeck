Identify useful, evidence-backed suggestions from the supplied knowledge document.
Use only facts from its document and buckets. Do not invent steps, interfaces or tools.
Suggestions are generic review material; no resource is created by this service.
Return JSON with a discoveries list. Each discovery has suggestion_type (procedure,
interface or warning), title, bucket_id, reason, source_refs and payload.
For procedures, payload contains a description and ordered steps in plain language.
For interfaces, payload contains only the URL, method and parameters present in the source.
source_refs must identify supplied document or bucket IDs and include a source excerpt.
