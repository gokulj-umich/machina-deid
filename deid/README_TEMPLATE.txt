Machina De-ID - Team Gocool
===========================

WHAT THIS IS
corpus/  de-identified copy of the full input corpus (every input file has an
         output file; nothing was dropped)
code/    complete pipeline source (Python 3.12) and pinned requirements

HOW TO RUN (Ubuntu 24.04, 8 vCPU, ~600 GB disk)
  sudo apt-get install -y python3-venv
  python3 -m venv .venv && . .venv/bin/activate
  pip install -r code/requirements.txt
  bash get-input.sh                      # downloads the corpus into ./input
  cd code && python3 -m deid.pipeline --input ../input --out ../attempt-N \
        --private ../private --workers 8
The private folder (secret pseudonym key, path map, run log) is created
outside the submission and is never uploaded.

METHOD
- Pseudonyms are deterministic: HMAC-SHA256 of the identity type and its
  normalised spelling with a private key. The same identity always gets the
  same pseudonym in every file, folder name and archive; different identities
  get different pseudonyms. Pseudonyms keep the identity type:
  OrgXXXXX (organisations), PersonXXXXX (people), OwnerXXXXX (repository
  owners), ProjectXXXXX (projects / repositories), site-xxxxx.example
  (domains), user-xxxxx@site-xxxxx.example (e-mail addresses).
- Identities are found from the corpus layout (repository owners and names,
  source domains), a reviewed list of organisations, people and projects
  named in collection titles, and inside files (STEP FILE_NAME author,
  organization and authorisation; STEP PERSON/ORGANIZATION entities; PDF
  author/company metadata and XMP; e-mail addresses; user-profile folders in
  embedded paths).
- File and folder names: only the identifying words are replaced; the rest of
  each name and every extension is kept.
- Text formats (STEP, DXF, SVG, VRML, OBJ, Markdown, source code...):
  identity strings replaced in place; geometry untouched.
- Length-sensitive and binary formats (IGES, ACIS SAT, Parasolid, STL,
  SolidWorks, Inventor, Solid Edge, Creo, CATIA, JT, images): replacements keep
  the exact byte length so record lengths, offsets and checksums stay valid.
- PDFs: document info and XMP rewritten with pseudonyms, visible text in page
  content streams replaced (length-preserving), attachments processed
  recursively.
- Archives (zip, tar, tar.gz, 7z) and zip-based formats (FCStd, 3MF, DOCX,
  XLSX): every member is processed with the same rules and re-packed; member
  paths inside .zip/.tar/.7z archives are renamed with the same rules as
  corpus paths.

KNOWN LIMITATIONS
- Native CAD files whose names are used as assembly/drawing links
  (SLDPRT/SLDASM/SLDDRW, IPT/IAM/IDW, PAR/ASM/PSM/DFT, PRT/ASM/DRW, CATIA,
  JT, X_T/X_B, DWG) keep their file names so links between drawings,
  models and assemblies keep resolving; their folders are renamed.
- SolidWorks 2015+ and similar compressed native formats: identities stored in
  compressed sections are not rewritten (no open SDK); plain-text and UTF-16
  occurrences are replaced length-preservingly.
- Logos and names drawn as images or vector outlines are not altered.
- RAR archives cannot be re-created without proprietary tools; their bytes are
  processed length-preservingly only.

STATISTICS
{{STATS}}

SOURCE DEFECTS AND EXCEPTIONS (final paths)
{{NOTES}}
