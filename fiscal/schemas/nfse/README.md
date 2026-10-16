# Official NFS-e schemas

Copied unchanged from the national package `nfse-esquemas_xsd-v1-01-20260209.zip`
(gov.br/nfse, documentação técnica), folder `Schemas/1.01`, with one local patch:

- `tiposSimples_v1.01.xsd`, type `TSSerieDPS`: the official pattern is `^0{0,4}\d{1,5}$`.
  XSD patterns are implicitly anchored, so `^` and `$` are literal characters there and the
  pattern rejects every real series. The anchors were removed.

Replace the files when a new package is published, and reapply the patch if it is still needed.
