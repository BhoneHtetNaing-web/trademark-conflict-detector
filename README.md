# Apexive AI Trademark Conflict Detector

## Supported comparisons

Upload two files in either source slot. The analyzer accepts PDF, XLSX, and XLSM files and supports:

- PDF against PDF
- XLSX/XLSM against XLSX/XLSM
- PDF against XLSX/XLSM (in either upload order)

PDF extraction is limited to artwork placed in the record's `(540)` field; spreadsheet extraction is limited to images anchored in a `Mark` (or equivalent logo/device) column. PDF/PDF and XLSX/XLSM-to-XLSX/XLSM comparisons use those same field-specific logo assets. Application-number equality and visual similarity are reported separately; similarity findings are technical evidence and require human/legal review. If both uploads are byte-for-byte identical, the field-specific logos are extracted once and compared as exact visual pairs instead of repeating the extraction.

## Local development

Run the backend from `backend` with `venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8020`, then run the frontend from `frontend` with `npm run dev`. The frontend's `VITE_API_URL` should be `http://127.0.0.1:8020`; restart Vite after changing it.

Completed jobs provide an XLSX match report, CSV/JSON results, and a summary. Same-format comparisons also include a ZIP of visual evidence; PDF-to-PDF comparisons provide marked copies of both PDFs. The `/api/analyze` endpoint accepts the `source_a` and `source_b` multipart fields; the earlier `government_pdf` and `client_xlsx` fields remain supported for existing clients.