#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
This script turns the contents of the IVOA document repository into
the ADS tagged format.

Warning: it will walk a major portion of the IVOA document repository,
which translates into ~100 requests fired without rate limitation.

Among the complications are:

(1) We're creating electronic document identifiers (see make_ivoadoc_id and
following)

(2) We're manipulating the author lists to ensure the editor(s) are in the
first position.

(3) As ADS would rather not have records they already have resubmitted,
we query it using a "new API" endpoint.

After all these complications, it might make sense to finally introduce
classes for representing records (rather than dictionaries, the keys of
which are defined through the namespace in the parse_landing_page
function...) and probably the whole collection, too (rather than a simple
list).  MD might do this if there's another feature request...


Distributed by the IVOA under CC0, https://spdx.org/licenses/CC0-1.0.html
"""

import argparse
import itertools
import json
import os
import re
import sys
import traceback
import urllib.parse

from bs4 import BeautifulSoup, NavigableString
import requests


CACHE_RESULTS = False

# When two documents were published on the same date from authors
# with the same initial, we need to reliably add a qualifier.
# This is a dict of landing page URLs to qualifiers.  In the future,
# the document coordinator should try to avoid such situations,
# so hopefully the following enumeration is exhaustive.
BIBCODE_QUALIFIERS = {
	"http://www.ivoa.net/documents/cover/ConeSearch-20080222.html": "Q",
	"http://www.ivoa.net/documents/VOSpace/20091007/": "Q",
	"http://www.ivoa.net/documents/SLAP/20101209/": "Q",
    "http://www.ivoa.net/documents/Coords/20221004/index.html": "Q",
    }

# endpoint of the ADS "bigquery" API
ADS_ENDPOINT = "https://api.adsabs.harvard.edu/v1/search/bigquery?"


########################## Utilties

class Error(Exception):
	"""Base class of exceptions raised by us.
	"""

class ValidationError(Error):
	"""is raised for documents that are in some way invalid.
	"""

class ExternalError(Error):
	"""is raised if some external service behaved unexpectedly.
	"""

class Finished(Exception):
	"""used by the abstract collector to abort item collection in case of
	malstructured documents.
	"""
	def __init__(self, payload):
		self.payload = payload
		Exception.__init__(self, "Unexpected div")


def get_with_cache(url):
	cacheName = re.sub(r"[^\w]+", "", url)+".cache"
	if CACHE_RESULTS and os.path.exists(cacheName):
		doc = open(cacheName, "r", encoding="utf-8").read()
	else:
		doc = requests.get(url).text
		if CACHE_RESULTS:
			with open(cacheName, "w", encoding="utf-8") as f:
				f.write(doc)
	return doc


def get_enclosing_element(soup, tag, text):
	"""returns the first match of tag that contains an element containg
	text.
	"""
	for el in soup.findAll(tag):
		if text in el.text:
			return el


########################## Screen scraping landing pages

MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
	"July", "August", "September", "October", "November", "December"]
DATE_RE = re.compile(r"(\d{1,2})\s*(%s)\s*(\d\d\d\d)"%
	"|".join(MONTH_NAMES))


def parse_subhead_date(s):
	"""returns first year, month, and day for a date as on IVOA document
	landing pages.
	"""
	mat = DATE_RE.search(s)
	if not mat:
		raise Exception("No date visible in %s"%repr(s))
	return (int(mat.group(3)),
		MONTH_NAMES.index(mat.group(2))+1,
		int(mat.group(1)))
	

def format_abstract(el):
	"""returns plain text from a BeautifulSoup element.

	This traverses the tree, stopping when it encounters the first div.
	Only very little markup is supported (all we have is ADS' abstract
	syntax).
	"""
	accum = []

	if isinstance(el, NavigableString):
		accum.append(el.string)

	elif el.name=="div":
		# this is probably bad document structure, in that this div
		# should not be a child of the abstract.  Stop collecting, but
		# pass upstream what we've collected so far.
		raise Finished(" ".join(accum))

	elif el.name in ("ul", "ol"):
		# can't see a way to properly do ul in running text, so folding
		# it to ol.
		for index, child in enumerate(el.findAll("li", recursive=False)):
			accum.append(" (%s) %s "%(index+1, format_abstract(child)))

	else:
		if el.name=="p":
			accum.append("\n\n")
		for child in el:
			try:
				accum.append(format_abstract(child))
			except Finished as rest:
				raise Finished(" ".join(accum+[rest.payload]))
	
	return " ".join(accum)


def get_abstract_text(soup):
	"""returns a guess for what the abstract within soup is.

	Unfortunately, the abstract isn't marked up well on IVOA landing
	pages.  Hence, we just look for the headline and gobble up material until
	we reach a div after that.
	"""
	abstract_head = get_enclosing_element(soup, "h2", "Abstract")
	el = abstract_head.nextSibling
	accum = []
	while getattr(el, "name", None)!="div":
		try:
			accum.append(format_abstract(el))
		except Finished as rest:
			# div found as abstract child, suspect malformed document.
			accum.append(rest.payload)
			break
		el = el.nextSibling
	return " ".join(accum)


def clean_field(s):
	"""return s with normalised space and similar, ready for inclusion
	into ADS' tagged format.

	Don't do this to abstracts.
	"""
# Oh shucks, "Grid *and* Web Services" requires a special hack.
	return re.sub(",? and ", ", ",
		re.sub(r"\s+", " ", s)).replace("Grid, ", "Grid and")


SHORT_NAME_EXCEPTIONS = {
	"VOT": "VOTable"
}

def guess_short_name(url_in_docrepo):
	"""guesses the short name of a document based on its docrepo URL.

	Due to historically confusing practices, this is hard to do.  Our
	heuristics: we throw out known parts of common URLs and take the
	segments that's the longest.
	>>> guess_short_name("http://www.ivoa.net/documents/SAMP/20120411/")
	'SAMP'
	>>> guess_short_name("www.ivoa.net/documents/cover/SAMP-20090421.html")
	'SAMP'
	>>> guess_short_name("http://www.ivoa.net/documents/cover/VOT-20040811.html")
	'VOTable'
	"""
	# cut prefix
	local_path = re.sub(".*documents/", "", url_in_docrepo)
	# cut known junk
	unjunked = re.sub("index.html", "",
		re.sub("cover/", "", local_path))
	# score candidates according to
	scored = list(sorted((
			len(re.sub("[^A-Z]+", "", s))+len(re.sub("[^a-z]+", "", s))/5.
			, s)
		for s in re.split("[/-]", unjunked)))
	# fail if inconclusive
	if len(scored)>1 and scored[-1][0]==scored[-2][0]:
		raise Error("Cannot infer short name: %s"%url_in_docrepo)
	
	return SHORT_NAME_EXCEPTIONS.get(scored[-1][1], scored[-1][1])
	

def parse_landing_page(url, local_metadata):
	"""returns a dictionary of document properties for a document taken from
	its landing page.
	"""
	soup = BeautifulSoup(get_with_cache(url), 'html5lib')
	authors = clean_field(
		get_enclosing_element(soup, "dt", "Author(s):"
			).findNextSibling("dd").getText(" "))
	editors = clean_field(get_enclosing_element(soup, "dt", "Editor(s):"
			).findNextSibling("dd").getText(" "))
	tagline = soup.find("h2").text
	date = parse_subhead_date(tagline)
	abstract = get_abstract_text(soup).replace("\r", "")

	title = clean_field(soup.find("h1").getText(" "))
	journal = tagline

	pdf_enclosure = get_enclosing_element(soup, "a", "PDF")
	if pdf_enclosure:
		pdf = urllib.parse.urljoin(url, pdf_enclosure.get("href"))

	try:
		arXiv_id = local_metadata.get_arXiv_id_for_URL(url)
	except KeyError:
		# That's ok for notes, and checked separately for RECs
		pass

	del soup
	return locals()


########################## Screen scraping the index page

def iter_links_from_table(src_table, rec_class):
	"""returns rec-like URLs from src_table.

	src_table is a BeautifulSoup node for one of our documents-in-progress
	tables (realistically, recommendations or endorsed notes).

	rec_class is a CSS class name which marks links to finished standards
	in the respective table (in reality, en or rec).

	The function yields anchor elements.
	"""
	for links in src_table.findAll("td", {"class": "versionold"}):
		for anchor in links.findAll("a", {"class": rec_class}):
			yield anchor


def iter_REC_URLs(doc_index, repo_url):
	"""iterates over URLs to RECs (different versions are different documents).

	doc_index is a BeautifulSoup of the IVOA documents repo.  Each URL
	in a class=rec anchor will be returned exactly once.  Document
	order is maintained.
	"""
	seen_stds = set()
	rec_table = get_enclosing_element(doc_index, "h3",
		"Technical Specifications").findNextSibling("table")
	en_table = get_enclosing_element(doc_index, "h3",
		"Endorsed Note").findNextSibling("table")

	for anchor in itertools.chain(
			iter_links_from_table(rec_table, "rec"),
			iter_links_from_table(rec_table, "ucd-en"),
			iter_links_from_table(en_table, "en")):
		# we'll fix URLs to some degree here; in particular,
		# uppercase Documents, which was fairly common in the old days,
		# is lowercased.
		url = urllib.parse.urljoin(repo_url, anchor.get("href"
			).replace("Documents", "documents"))

		if url in seen_stds:
			continue
		seen_stds.add(url)
		yield url


def iter_Notes_URLs():
	"""iterates over URLs of published notes.

	Right now, most notes are not pushed to ADS.  Instead, the exec
	lists the ones it wants published, and the document coordinator
	manually adds the URLs to published_notes.txt.
	"""
	with open("published_notes.txt") as f:
		for ln in f:
			if ln.strip() and not ln.startswith("#"):
				yield ln.strip()


########################## record generation logic

def parse_authors(literal):
	"""returns authors from literal as a list.

	This understands First1 Last1, First2 Last2 as well as
	Last1, F.; Last2, J. formats.

	As a sanity check, this will bomb out when there is no blank in
	any particle.  Admittedly, that's very western-centric, but let's
	discuss that when we get into trouble with this assumption.

	>>> parse_authors("Last, J.; Greger, Max")
	['Last, J.', 'Greger, Max']
	>>> parse_authors('Greg Ju, Fred Gnu Test, Wang Chu')
	['Greg Ju', 'Fred Gnu Test', 'Wang Chu']
	>>> parse_authors("Messy, this.")
	Traceback (most recent call last):
	ValueError: Unlikely author name 'Messy'
	"""
	if re.search(r"[A-Z]\.$", literal) or ";" in literal:
		res = literal.split(";")
	else:
		res = literal.split(",")

	res = [s.strip() for s in res]
	for part in res:
		if not " " in part:
			raise ValueError(f"Unlikely author name '{part}'")
	
	return res


class Document(dict):
	"""Metadata of an IVOA document.

	These are constructed with a dictionary of items found; this
	includes authors*, editors*, date*, abstract*, title*, type*
	(spec/rept), pdf (its URL), url* (of the landing page), journal*,
	arXiv_id (mandatory for RECs), but additional arbitrary keys are allowed.
	Items with stars are mandatory.

	You'll usually use the from_URL class function to construct one
	from an IVOA document landing page.

	>>> Document(TEST_DATA["ru"])
	Traceback (most recent call last):
	harvest.ValidationError: Document at http://foo/bar: Missing key(s) date, editors
	>>> d = Document(TEST_DATA["r1"])
	>>> d["authors"]
	'Greg Ju, Fred Gnu Test, Wang Chu'
	>>> d.bibcode
	'2014ivoa.spec.0307J'
	>>> d.as_ADS_record()[:59]
	'%R 2014ivoa.spec.0307J\\n%D 3/2014\\n%I ELECTR: http://foo/bar;'
	>>> Document(TEST_DATA["rme"])["authors"]
	'Editor, First; Editor, S.; Guy, S.; Rixon, G.'
	>>> d2 = Document.from_URL("http://www.ivoa.net/documents/SAMP/20120411"
	...   "/index.html", TEST_DATA["lm"])
	>>> d2["authors"]
	'T. Boch, M. Fitzpatrick, M. Taylor, A. Allan, J. Fay, L. Paioro, J. Taylor, D. Tody'
	>>> d2.bibcode
	'2012ivoa.spec.0411B'
	"""

	mandatory_keys = frozenset(
		["url", "authors", "editors", "date", "abstract", "title", "journal"])
	key_to_ads = [
		("authors", "A"),
		("editors", "e"),
		("title", "T"),
		("source", "G"),
		("journal", "J"),
		("abstract", "B"),
	]

	def __init__(self, vals):
		dict.__init__(self, vals)
		self["source"] = "IVOA"
		self.validate()
		self._perform_editor_hack()
		self._infer_type()
#		if self["type"]=="spec":
#			if not self.get("arXiv_id"):
#				raise Error("RECs must have arXiv_id (add to arXiv_ids.txt);"
#					" failing on document at %s"%(self["url"]))

	@classmethod
	def from_URL(cls, url, local_metadata):
		"""returns a new Document made from the IVOA landing page at url.
		"""
		return cls(parse_landing_page(url, local_metadata))

	def validate(self):
		"""raises a ValidationError if one or more of the mandatory_keys
		are missing.
		"""
		missing_keys = self.mandatory_keys-set(self)
		if missing_keys:
			raise ValidationError("Document at %s: Missing key(s) %s"%(
				self.get("url", "<unknown origin>"), ", ".join(sorted(missing_keys))))

	def _infer_type(self):
		"""decides whether this document is a spec (Recommendation) or
		rept (Note).

		We currently do this according to the journal field (specs have
		"Recommendation" or "Endorsed Note" in there).
		"""
		if ("Recommendation" in self["journal"]
				or "Endorsed Note" in self["journal"]):
			self["type"] = "spec"
		else:
			self["type"] = "rept"

	def _perform_editor_hack(self):
		"""fudges the authors list to include the editor(s) in the first place.

		This was the express wish of Francoise Genova to provide sufficient
		credit to the editors who, typically, did most of the work that went
		into a document.

		This method is called by the constructor; it's designed to be
		idempotent.
		"""
		if not self["editors"].strip():
			return

		eds = parse_authors(self["editors"])
		auths =  parse_authors(self["authors"])

		non_editors = [item for item in auths if item not in eds]
		if non_editors:
			auths = eds+non_editors

		# unparsing: use ; as a separator if there is a comma in the first
		# author (see parse_authors for the rationale).
		if "," in auths[0]:
			self["authors"] = "; ".join(auths)
		else:
			self["authors"] = ", ".join(auths)

	_exceptional_surnames = {
		"Preite Martinez"}

	def get_first_author_surname(self):
		"""returns the surname for the first author.

		This is pure heuristics -- we need it for bibcode generation, and
		hence we should keep this in sync with what ADS wants.
		"""
		# current heuristics for First Last-format authors: first character of last
		# "word" of the first token parsed from authors (after the editor hack).
		# This will fail for surnames consisting of multiple tokens.  We collect
		# these in the _exceptional_surnames set above.

		first_author = parse_authors(self["authors"])[0]
		if "," in first_author:
			# we're in luck: Last, F. format
			return first_author.split(",")[0]

		for exception in self._exceptional_surnames:
			if exception in first_author:
				return exception

		return first_author.split()[-1]

	@property
	def bibcode(self):
		"""returns the bibcode for this record.
		"""
		year, month, day = self["date"]
		return "%sivoa.%s%s%02d%02d%s"%(
			year, self["type"],
			BIBCODE_QUALIFIERS.get(self["url"], "."),
			month, day,
			self.get_first_author_surname()[0])

	def as_ADS_record(self):
		"""returns ADS tagged format for doc_dict as returned
		by our parsers.
		"""
		parts = ["%%R %s"%self.bibcode]

		year, month, day = self["date"]
		parts.append("%%D %s/%s"%(month, year))

		links = "%%I ELECTR: %s"%self["url"]
		if "pdf" in self:
			links += ";\nPDF: %s"%self["pdf"]
		if "ivoadoc-id" in self:
			links += ";\nEPRINT: %s"%self["ivoadoc-id"]
		if "arXiv_id" in self:
			links += ";\nARXIV: %s"%self["arXiv_id"]
		parts.append(links)
			
		for our_key, ads_key in self.key_to_ads:
			if our_key in self:
				parts.append("%%%s %s"%(ads_key, self[our_key]))

		return "\n".join(parts)


class DocumentCollection(object):
	"""A collection of IVOA document metadata.

	This also contains logic that needs to see the entire collection.

	It is constructed with a sequence of Document instances; you
	will usually use the from_repo_URL class method which takes the
	URL of the IVOA's document collection.

	These things are conceptually immutable (i.e., you're not supposed
	to change self.docs).

	The main interface to this is iteration -- you'll get all the
	documents in temporal order.

	>>> dc = DocumentCollection(
	...   Document(TEST_DATA[k]) for k in "r1 r2 r3".split())
	>>> dc.docs[0].bibcode
	'2014ivoa.spec.0307J'
	"""
	def __init__(self, docs):
		self.docs = list(docs)
		self._sort_recs()
		self._create_identifiers()
		self.validate()

	@classmethod
	def from_repo_URL(cls, root_url, local_metadata):
		"""returns a DocumentCollection ready for export, constructed
		from the index at root_url.
		"""
		doc_index = BeautifulSoup(requests.get(root_url).text, 'html5lib')
		docs = []
		
		for url in itertools.chain(
				iter_REC_URLs(doc_index, root_url),
				iter_Notes_URLs()):
			try:
				docs.append(
					Document.from_URL(urllib.parse.urljoin(root_url, url), local_metadata))
			except KeyboardInterrupt:
				raise
			except:
				sys.stderr.write("\nIn document %s:\n"%url)
				traceback.print_exc()
		return cls(docs)
	
	def __iter__(self):
		return iter(self.docs)

	def validate(self):
		"""runs some simple tests to avoid certain undesirable situations.

		Problems will lead to a validation error being raised.
		"""
		docs_per_bibcode = {}
		for doc in self:
			docs_per_bibcode.setdefault(doc.bibcode, []).append(doc)
		dupes = [item for item in docs_per_bibcode.items()
			if len(item[1])>1]
		if dupes:
			raise ValidationError("The following documents generated"
				" clashing bibcodes: %s.  Fix by adding one of them to"
				" BIBCODE_QUALIFIERS in the source."%(
					" AND ALSO\n".join(
						" and ".join(c["url"] for c in clashing[1])
							for clashing in dupes)))

	def _make_ivoadoc_id(self, rec, index):
		"""returns, for a rec as returned by parse_landing_page
		and the document index within the publication month,
		the document's IVOA document id.

		The IVOA document id  has the form
		ivoa:<t>.<year>.<month>.<count>.  count is a running
		number per month, where documents are sorted first
		by date, then by first author last name, and finally
		by title.  <t> is r for a REC-type thing, n for a
		NOTE-like thing.

		This is a helper for _create_identifiers.
		"""
		return "ivoa:%s.%04d.%02d.%02d"%(
			"r" if rec["type"]=="spec" else "n",
			rec["date"][0],
			rec["date"][1],
			index)

	def _sort_recs(self):
		"""sorts our records as required for IVOA identifier generation.

		That is, sorted by date, authors, and titles, in that order.
		This is called by the constructor.
		"""
		self.docs.sort(key=lambda rec: rec["date"]+(
			rec.get_first_author_surname(), rec["title"]))

	def _get_month_partition(self):
		"""returns a dictionary mapping (year, month) to the documents published
		in that month

		This is a helper for _create_identifiers
		"""
		by_month = {}
		for rec in self.docs:
			year, month, day = rec["date"]
			by_month.setdefault((year, month), []).append(rec)
		return by_month

	def _create_identifiers(self):
		"""adds ivoadoc-id keys to every record in self.

		See _make_ivoadoc_id for what this is.

		This is called by the constructor.
		"""
		for (year, month), recs in self._get_month_partition().items():
			for index, rec in enumerate(d for d in self.docs if d["type"]=="spec"):
				rec["ivoadoc-id"] = self._make_ivoadoc_id(rec, index)
			for index, rec in enumerate(d for d in self.docs if d["type"]=="rept"):
				rec["ivoadoc-id"] = self._make_ivoadoc_id(rec, index)

	@staticmethod
	def _guess_short_name(doc_uri):
		"""returns the IVOA short name, lowercased, from a document repository
		URI.

		This is not really well-defined due to varying historical practices.
		Our heuristics is: go for "documents", take the next segment; if that
		is in a stopword list, take still the next segment, otherwise return it.
		"""
		parts = doc_uri.lower().split("/")
		after_documents = False
		stop_words = {'notes', 'cover'}

		for p in parts:
			if after_documents:
				if p in stop_words:
					continue
				else:
					return p

			if p=="documents":
				after_documents = True

		raise ValueError(f"Docrepo URI without 'document': {doc_uri}")

	def get_bibcode_mapping(self):
		"""returns a dictionary of document short names to bibcodes for it.
		"""
		shortname_to_bibcodes = {}
		for doc in self.docs:
			shortname_to_bibcodes.setdefault(
				self._guess_short_name(doc["url"]), []).append(doc.bibcode)

		return shortname_to_bibcodes


########################## local metadata injection

class LocalMetadata(object):
	"""A container for parsed metadata from kept in the github repo.

	Currently, that's a mapping from document short names to arXiv ids, kept in
	arXiv_map.  By Exec decree, this is only available for IVOA RECs.
	"""
	def __init__(self):
		self._load_arXiv_map()
	
	def _load_arXiv_map(self):
		self.arXiv_map = {}
		with open("arXiv_ids.txt") as f:
			for ln_index, ln in enumerate(f):
				try:
					if ln.strip():
						access_URL, arXiv_id = ln.split()
						self.arXiv_map[access_URL.strip()] = arXiv_id.strip()
				except ValueError:
					sys.exit("arXiv_ids.txt, line %s: entry not in <local><white><arxiv>"
						" format."%(ln_index+1))

	def get_arXiv_id_for_URL(self, url):
		"""returns the arXiv id based on a URL into the document repository.
		
		This involves guessing the short name, which may fail for weirdly formed
		docrepo URLs.

		If the lookup itself fails, a KeyError with the original url is raised.
		"""
		short_name = guess_short_name(url)
		if short_name in self.arXiv_map:
			return self.arXiv_map[short_name]
		raise KeyError(url)


########################## ADS interface

def filter_unpublished_bibcodes(bibcodes, auth):
	"""returns a list of bibcodes not yet known to ADS from bibcodes.
	"""
	params = {
		'q': '*:*',
		'rows': 1000,
		'wt': 'json',
		'fq': '{!bitset}',
		'fl': 'bibcode'}
	payload = "bibcode\n"+"\n".join(bibcodes)

	req = requests.post(ADS_ENDPOINT,
		params=params,
		headers={'Authorization': 'Bearer %s'%auth},
		data=payload)
	response = json.loads(req.text)

	if response["responseHeader"]["status"]!=0:
		raise ExternalError("ADS API returned error: %s"%repr(response))

	known_bibcodes = set([r["bibcode"] for r in response["response"]["docs"]])
	for bibcode in bibcodes:
		if not bibcode in known_bibcodes:
			yield bibcode


########################## command line interface

def _test():
	"""runs the embedded doctests.
	"""
	import doctest, harvest
	harvest.TEST_DATA = {
		"r1": {"url": "http://foo/bar", "title": "Test doc",
			"authors": "Fred Gnu Test, Wang Chu", "editors": "Greg Ju",
			"date": (2014, 3, 7), "abstract": "N/A", "pdf": "uh",
			"journal": "IVOA Recommendation", "arXiv_id": "a-p/1"},
		"r2": {"url": "http://foo/baz", "title": "More Testing",
			"authors": u"René Descartes", "editors": "J.C. Maxwell",
			"date": (2014, 3, 7), "abstract": "N/A",
			"journal": "IVOA Recommendation", "arXiv_id": "a-p/2"},
		"r3": {"url": "http://foo/quux", "title": "Still more",
			"authors": "Leonhard Euler, Georg Cantor",
			"editors": "Frederic Chopin",
			"date": (2014, 5, 7), "abstract": "N/A",
			"journal": "IVOA Note"},
		"ru": {"url": "http://foo/bar", "title": "Test doc",
			"journal": "Broken Mess", "abstract": "", "authors": "X"},
		"rr": {"url": "http://foo/failrec", "title": "Test REC",
			"authors": "Fred Gnu Test, Wang Chu", "editors": "Greg Ju",
			"date": (2014, 3, 7), "abstract": "N/A", "pdf": "uh",
			"journal": "IVOA Recommendation"},
		"rme": {"url": "http://foo/twoeditors", "title": "I have two editors",
			"authors": "Editor, S.; Guy, S.; Rixon, G.; Editor, First",
			"editors": "Editor, First; Editor, S.",
			"date": (2014, 3, 20), "abstract": "N/A",
			"journal": "IVOA Note"},
		"lm": LocalMetadata(),
			}
	doctest.testmod(harvest)


def parse_command_line():
	parser = argparse.ArgumentParser(
		description="Generate ADS records from the IVOA document repo.")
	parser.add_argument("-r", "--repo-url",
		action="store", dest="repo_url",
		help="Use URL as the document repository's URL",
		metavar="URL", default="http://www.ivoa.net/documents/")
	parser.add_argument("-t", "--test-only",
		action="store_true", dest="run_tests",
		help="Only run doctests, then exit (requires network).")
	parser.add_argument("-C", "--use-cache",
		action="store_true", dest="cache_web",
		help="Use cached copies of things obtained from the net"
			" (or create these caches).")
	parser.add_argument("-a", "--ads-token",
		action="store", type=str, dest="ads_token",
		help="ADS access token to filter out records already in ADS.",
		default=None)
	parser.add_argument("-s", "--single-doc",
		action="store", dest="doc_url",
		help="Only translate document with landing page url URL (only for"
			" testing/debugging; bibcodes may be wrong).",
		metavar="URL", default=None)
	return parser.parse_args()


def main():
	global CACHE_RESULTS
	args = parse_command_line()
	if args.cache_web or args.run_tests:
		CACHE_RESULTS = True

	if args.run_tests:
		_test()
		return

	local_metadata = LocalMetadata()
	if args.doc_url:
		dc = DocumentCollection(
			[Document.from_URL(args.doc_url, local_metadata)])
	else:
		dc = DocumentCollection.from_repo_URL(
			args.repo_url, local_metadata)

	with open("bibcode_mapping.json", "w") as f:
		json.dump(dc.get_bibcode_mapping(), f)

	limit_to = None
	if args.ads_token:
		limit_to = set(filter_unpublished_bibcodes(
			[doc.bibcode for doc in dc], args.ads_token))
	
	for rec in dc:
		if limit_to is not None:
			if rec.bibcode not in limit_to:
				continue

		sys.stdout.buffer.write(rec.as_ADS_record().encode("utf-8"))
		sys.stdout.write("")
	

if __name__=="__main__":
	try:
		main()
	except ValidationError as msg:
		sys.stderr.write(str(msg)+"\n")
		sys.stderr.write(
			"\nDocument repository invalid, not generating records.\n")
