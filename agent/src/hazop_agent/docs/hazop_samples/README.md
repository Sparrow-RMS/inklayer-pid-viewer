# HAZOP reference reports

Put past HAZOP study reports here (PDF with a text layer, Excel, Word, CSV or
text): reports whose worksheets were accepted, written the way your reviewers
expect. Then build the reference library:

    uv run hazop-agent samples            # reads new or changed reports only
    uv run hazop-agent samples --force    # reads every report again

That writes `library.json` here: each report's worksheet rows in its own
wording, and a style guide drawn from all of them. Step 4 ("Generate
worksheet") then writes causes, consequences, safeguards and recommendations
in that style, with the rows for the same deviations as examples.

The reports and `library.json` are kept out of git (see .gitignore): they are
client documents. Each report is sent to the model once, when the library is
built; the matching rows go with every worksheet drafted.
