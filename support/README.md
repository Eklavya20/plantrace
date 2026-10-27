# Reproducibility assets

- `evaluation/fixtures/`: controlled vector PDF sources, geometry labels and the original synthetic source reference.
- `evaluation/fixtures-stage6/`: additional partial-wall source and oracle.
- `evaluation/reports/`: compact recorded results for the focused workflow and reliability evaluation.
- `evaluation/real-plan-inventory-stage6.json`: source/permission metadata and failed independent-source access attempts; no real-plan result is claimed.
- `evaluation/import-template.json`: blank independent-input manifest template.
- `reproducibility/requirements-lock.txt`: exact dependency snapshot from the tested Windows/Python 3.11 environment.

Run workflows from the repository root. New reports, images and test databases are written to `support/artifacts/`, which is excluded from Git. Original synthetic source bytes are unchanged. Model/review databases from local use are not included.
