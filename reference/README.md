# Reference documents

Anything in this folder shows up on the dashboard's **Reference** tab, for
whoever is reviewing leads to check against — territory maps, price lists,
product sheets.

The quickest way in is the **Reference tab itself**: take a screenshot and
paste it on the page with Ctrl+V, drag files onto the add box, or pick them
with the file chooser. Copying files straight into this folder works exactly
the same way — the tab reads whatever is here.

- **Images** (`.png .jpg .jpeg .gif .webp .bmp`) and **PDFs** are displayed on
  the page. **`.txt .md .csv`** are shown as text. Anything else is listed with
  a download link.
- **Subfolders become sections.** A `Territories/` folder groups everything in
  it under a "Territories" heading.
- The file name is the heading, with dashes and underscores read as spaces:
  `CRR_Territory_Map.png` shows as "CRR Territory Map".

The tab lists documents by filename; clicking one opens its preview, and the
**Delete** button inside that preview removes the file. Deleting is therefore
never blind — you are looking at the document when you do it.

An upload never overwrites: adding a second `map.png` becomes `map (2).png`,
so replacing a document means deleting the old one on purpose.

This README is not listed on the tab. It explains the folder rather than being
reference material, and the tab refuses to delete it.

`.svg` and `.html` cannot be uploaded and are not displayed on the page. An SVG
is an image to a person but a scriptable document to a browser, and these files
are served from the dashboard's own origin. Export those as PNG or PDF instead.

The contents of this folder are **not committed to git** (only this README is).
These are internal sales documents, and the repository is not where they
belong — the same reasoning that keeps `input/` and `email_messages/` out.
