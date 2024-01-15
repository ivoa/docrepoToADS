This directory contains software to turn the contents of the IVOA
document repository into the tagged format of ADS.

It is probably mainly of interest to the IVOA document coordinator.

Most of the information comes from the document repository landing pages
right now.  Additionally, there are the following resources:

* arXiv_ids.txt -- an accessURL/arXiv id mapping maitained by the
  document coordinator.
* published_notes.txt -- a list of landing page URLs with notes intended
  for publication; the exec names the notes to be published.
* (ads) -- via its API, we check what records were already uploaded to ADS
  to avoid inundating them with dupes.


Dependencies
============

python3, beautifulsoup (Debian systems: python3-bs4), requests
(Debian systems:python3-requests), html5lib (Debian: python3-html5lib).


The Editor Hack
===============

The Exec insisted we have to manipulate author lists to recognise that
for IVOA documents, most of the work is done by the editor.  Therefore,
the script takes the editor names, removes them from the author list if
necessary, and then prepends them to the rest of the list.


Identifiers
===========

This script generates two sorts of identifiers:

(a) bibcodes.  The bibcodes we generate use spec as bibstem for
recommendations (which are considered refereed) and rept as bibstem for
notes (which are considered unrefereed).  The "volume" is month and day
of publication.  Where the same editor uploaded a document on the same
month and day, qualifiers are used to disambiguate.

(b) IVOA eprint ids.  These are not really used anywhere at the moment
but might become a tool to manage the document collection in the future.
They have the form ivoa:<r|n>.<year>.<month>.<count>, where count starts
from 0 each month and runs separately for each document type; r is for
recommendation, n for note.


ADS interface
=============

To avoid uploading records that ADS already has, you should obtain an
ADS API token (see https://github.com/adsabs/adsabs-dev-api).  When
generating records for submission, pass in this token through the -a
option.


Brief HOWTO
===========

Just run::

	python harvest.py -C -a your-access-token > ads.recs

[recommendation: set the token in your environment and run::

  rm -f httpwwwivoanetdocuments.cache
	python3 harvest.py -C -a $ADS_TOKEN > ads.recs
]

Send ads.recs to ADS.


Open issues
============

Can we sanely extract references?  Maybe at least for ivoatex-processed
documents?  In the latter case, that would be easy if we wanted to run
TeX, as it's all BibTeX then.  But frankly, I'd rather not run TeX as
part of this procedure.  Perhaps we should have a separate procedure for
the references?
