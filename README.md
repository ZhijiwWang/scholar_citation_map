# Scholar Citation Map

Turn a Google Scholar profile into an interactive world map of the institutions
represented among its citing authors. Export the underlying citation, affiliation,
and location records as CSV files for review or further analysis.

The map groups nearby coordinates and uses darker blue for higher frequencies.
**A frequency of 10 or more uses the darkest color.** Higher-frequency points are
drawn last, so they appear above lower-frequency points.

## How it works

1. Retrieve the profile's papers and their citing papers from Google Scholar,
   either directly or through SerpApi.
2. Match citing papers to OpenAlex and Crossref metadata to retrieve author
   affiliations. When metadata is incomplete, optionally use Gemini to extract
   affiliations from public publisher pages or PDF text.
3. Use Gemini on Google Cloud, authenticated with Application Default Credentials
   (ADC), to identify each institution's city and city-center coordinates. Google
   Search grounding is enabled by default.
4. Preserve repeated affiliations in CSV and generate a map with an embedded
   Natural Earth world basemap.

This is a command-line Python project. No web server or map API key is required.

## Requirements

- Python 3.10 or newer; Python 3.12 is recommended.
- A Google Cloud project with billing, the Vertex AI API
  (`aiplatform.googleapis.com`), and permission to call Gemini.
- The [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) for local ADC setup.
- A [SerpApi key](https://serpapi.com/manage-api-key) if using the SerpApi provider.
  Direct Scholar access is available but may be blocked by a CAPTCHA.

Gemini and SerpApi usage may incur charges. Institution addresses are sent to
Gemini; publisher extraction also sends the retrieved paper text.

## Quick start

Run the following commands from the project directory. Replace `YOUR_PROJECT_ID`
and `YOUR_USER_ID` with your own values.

### 1. Install dependencies

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### 2. Configure Gemini authentication

Make sure `gcloud` is available in your terminal, then run:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Complete the Google sign-in in your browser. On macOS or Linux, the included
`bash scripts/setup_adc.sh YOUR_PROJECT_ID` runs these authentication steps for you.

Your account needs permission to invoke Gemini (for example, `roles/aiplatform.user`)
and `serviceusage.services.use` on the quota project. Ask your project administrator
if these permissions or API access are managed by your organization.

ADC manages access tokens automatically. With the standard local login above,
leave `GOOGLE_APPLICATION_CREDENTIALS` unset. If your organization supplies a
federation or other ADC configuration file, set that variable to its absolute path.
See Google's [local ADC guide](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).

### 3. Edit `.env`

```dotenv
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=global
GEMINI_MODEL=gemini-2.5-flash
SERPAPI_API_KEY=YOUR_SERPAPI_KEY
OPENALEX_API_KEY=
CONTACT_EMAIL=
```

| Setting | Purpose |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID used for Gemini and its quota project |
| `GOOGLE_CLOUD_LOCATION` | Model region; defaults to `global` |
| `GEMINI_MODEL` | Gemini model ID; override per command with `--model` |
| `SERPAPI_API_KEY` | Required only when passing `--provider serpapi` |
| `OPENALEX_API_KEY` | Optional key for OpenAlex metadata requests; see [OpenAlex authentication](https://help.openalex.org/api/authentication/) |
| `CONTACT_EMAIL` | Optional contact email sent to OpenAlex |

The default model is `gemini-2.5-flash`. Google currently lists its retirement date
as October 20, 2026; update `GEMINI_MODEL` to a model available in your project when
needed. Consult the [model documentation](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/2-5-flash).

### 4. Run a small first pass

```bash
python scholar_citation_map.py run \
  'https://scholar.google.com/citations?user=YOUR_USER_ID' \
  --provider serpapi --max-papers 1 --max-citations 5
```

On PowerShell, enter the command on one line instead of using Bash line continuation.
The limits intentionally produce a partial run, which can return exit code `2`.
Review `output/` before removing the limits:

```bash
python scholar_citation_map.py run \
  'https://scholar.google.com/citations?user=YOUR_USER_ID' \
  --provider serpapi
```

To access Scholar directly, omit `--provider serpapi`. Setting a SerpApi key alone
does not enable that provider. Direct retrieval stops when Scholar requires a CAPTCHA.

### 5. Open the map

Open `output/world_map.html` in a browser. Hover over or click points to inspect
frequencies, institutions, and citing-paper counts.

The boundaries and data are embedded in the HTML; no public map-tile requests are
made. JavaScript and CSS still load from external CDNs, so internet access is needed
unless the browser has already cached those assets.

## Output files

All commands default to `output/`. Use `--output-dir PATH` to choose another folder.
CSV files use UTF-8 with a byte-order mark for spreadsheet compatibility.

| File | Contents |
| --- | --- |
| `source_papers.csv` | Profile papers, including papers with no citations |
| `citation_edges.csv` | Relationships between profile papers and citing papers |
| `affiliations.csv` | Author affiliations, source evidence, and missing-data status |
| `institutions_geocoded.csv` | Affiliation rows plus city, country, coordinates, confidence, and source URLs |
| `coordinate_frequency.csv` | Aggregated coordinates, frequencies, and institution/paper counts |
| `world_map.html` | Interactive world map |
| `crawl_report.json` | Retrieval status, citation shortfalls, and affiliation coverage |
| `geocode_report.json` | Counts of resolved, ambiguous, unknown, skipped, and failed locations |
| `map_report.json` | Mapping settings, eligible rows, and excluded records |

One affiliation row represents **profile paper × citing paper × author × institution**.
Two authors at the same institution produce two rows. A citing paper that references
two profile papers contributes rows to both citation relationships. Missing
affiliations remain in the CSV with a status rather than disappearing.

Important fields include `source_paper_*`, `citing_paper_*`, `author_name`,
`institution`, `raw_affiliation`, `affiliation_source`, and `affiliation_status`.
Geocoded rows add `city`, `region`, `country`, `latitude`, `longitude`,
`geocode_status`, `geocode_confidence`, `geocode_reason`, `geocode_sources`,
`geocode_model`, and `geocode_response_id`. Coordinates are WGS84 decimal degrees
for city centers, with latitude first. Source names and quoted evidence retain
their original language.

## Run individual stages

Retrieve affiliations without Gemini or publisher extraction:

```bash
python scholar_citation_map.py crawl \
  'https://scholar.google.com/citations?user=YOUR_USER_ID' \
  --provider serpapi --no-publisher-fallback
```

Resolve institutions from an existing affiliation CSV:

```bash
python scholar_citation_map.py geocode output/affiliations.csv
```

Redraw an existing geocoded CSV without calling any API:

```bash
python scholar_citation_map.py map output/institutions_geocoded.csv
```

You can correct affiliations before `geocode`, or correct coordinates and statuses
before `map`. Running `geocode` again rebuilds the location columns from model results.

## Mapping options

```bash
python scholar_citation_map.py map output/institutions_geocoded.csv \
  --cluster-km 10 --count-mode unique-papers --color-scale log
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--cluster-km` | `25` | Group coordinates connected by distances within this threshold; `0` groups identical coordinates only |
| `--count-mode` | `rows` | Count affiliation rows, `paper-institution` pairs, or `unique-papers` |
| `--min-confidence` | `medium` | Include resolved locations at or above `low`, `medium`, or `high` confidence |
| `--color-scale` | `linear` | Use linear or logarithmic color scaling, capped at frequency 10 |

`paper-institution` counts one institution per profile-paper/citing-paper relationship,
regardless of how many authors share it. `unique-papers` counts distinct citing papers
within each coordinate group, including across profile papers.

Clustering uses connected neighbors: if A is near B and B is near C, all three may
form one group even if A is farther from C than the threshold. Group coordinates
are spherical averages weighted by affiliation rows. Frequency counts remain exact
in popups and CSV even when their color saturates at 10.

## Caching and retries

Successful HTTP, affiliation, and geocoding results are stored in `.cache/` by default.
Repeated institution/address inputs can reuse one Gemini result while retaining all
CSV rows. Different campus/address contexts are resolved separately.

Rerunning traverses the profile again and reuses matching cached results; it does not
jump directly to a saved page cursor. Failed requests are retried when reached.
Output CSV files are rebuilt, not appended. Fix quota or credential errors before
retrying, keep the same cache folder, and do not use `--refresh` unless you want to
ignore cached results. Changed prompts or model settings can trigger new Gemini calls.

Other useful options:

- `--max-papers` and `--max-citations`: limit a run; `0` means unlimited.
- `--delay`: minimum seconds between HTTP requests to the same service; default `3`.
- `--no-web-search`: skip Google Search verification; confidence is capped at `medium`.
- `--cache-dir PATH`: choose a cache folder.
- `--verbose`: enable application debug logs.

Place global options before the subcommand:

```bash
python scholar_citation_map.py --env-file /path/to/settings.env run \
  'https://scholar.google.com/citations?user=YOUR_USER_ID' --provider serpapi
```

Use `python scholar_citation_map.py --help` or append `--help` to a subcommand
for the full option list. Do not run concurrent jobs that share an output folder.

## Troubleshooting and limitations

- **Scholar blocked / CAPTCHA:** cached results are retained. Retry later or switch
  to `--provider serpapi`; the script does not solve CAPTCHAs.
- **SerpApi error:** check that provider's key, quota, and search parameters. Changing
  Gemini authentication does not fix Scholar retrieval errors.
- **Missing ADC / permission denied:** repeat ADC setup and check the project,
  API access, billing, and model permissions. `gcloud auth login` alone does not
  create Application Default Credentials.
- **Slow startup:** dependency loading prints progress messages. If imports time out,
  recreate the virtual environment with a local Python installation.
- **Incomplete data:** Scholar counts can differ from accessible citation lists.
  OpenAlex/Crossref may lack affiliations; very long author lists may be truncated.
  Publisher pages can be inaccessible, and scanned PDFs need OCR, which is not included.
- **Location uncertainty:** ambiguous institutions or unsupported coordinates are
  left blank. Model confidence and source links are review aids, not proof of accuracy.

Publisher extraction reads up to the first five PDF pages, tries up to six URLs per
paper, and limits extracted text to 60,000 characters. Online geocoding uses two
Gemini requests per uncached institution context: search verification followed by
structured JSON extraction. Only source URLs returned by search grounding are accepted.

Exit codes are `0` for a finished command, `1` for input/configuration errors,
`2` for partial retrieval or geocoding failures, and `130` for interruption.
Always review the reports: a successful exit does not guarantee every affiliation
was found. If retrieval produces no affiliation rows and is incomplete, the full
pipeline stops before geocoding and map generation; any previous map remains unchanged.

## Project layout

```text
scholar_citation_map.py   Command-line entry point
scholar_map/             Retrieval, affiliation resolution, Gemini, and mapping code
scholar_map/data/        Embedded world boundaries and data provenance
prompts/                 English extraction and geocoding prompts
scripts/setup_adc.sh     Optional local ADC setup helper
.env.example             Configuration template with no credentials
requirements.txt         Python dependencies
```

Local `.env` files, credentials, virtual environments, caches, and default outputs
are ignored by Git. Keep custom output folders and credential files out of commits too.
The bundled Natural Earth dataset is public domain; see [data provenance](scholar_map/data/README.md).
