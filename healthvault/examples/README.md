# Example files

Fictional data, safe to import into a scratch record:

```
python -m healthvault --db /tmp/try.db import examples/fhir_bundle.json
python -m healthvault --db /tmp/try.db import examples/medical_aid_statement.csv
python -m healthvault --db /tmp/try.db import examples/my_allergies.csv
python -m healthvault --db /tmp/try.db review
```

Note what each importer refuses to conclude. The statement contains a
line for diabetic retinopathy screening; the claims importer stages it as
a *visit*, never as a diagnosis of diabetes. The FHIR bundle, which
states the diagnosis outright, does stage the condition — and at higher
confidence, because the field is typed rather than inferred.
