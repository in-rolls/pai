# Parser fixtures

| File | Source layout |
| --- | --- |
| `legacy_GVdataT_page.html` | `GVdataT` table from the unified PAI page, <https://pai.gov.in/PS/Public/TW-GP.aspx> |
| `flat_GVdata_page.html` | `GVdata` table from the historical flat PAI page, <https://pai.gov.in/PS/Public/TW-GP-New.aspx> |

These retained table excerpts were present at commit
`0b68a89f3fc03f1ab880f373ead99e3a47154373`. They do not include capture timestamps
or complete request parameters, so those details are unknown. The flat-page
fixture tests historical layout parsing; it does not establish that the retired
route supplies complete current data. Browser tests load the local fixtures.
Other tests construct synthetic records and fake responses in Python.
