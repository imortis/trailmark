from trailmark.text import BM25, MentionLinker, tokenize


def test_tokenize_splits_identifiers_and_drops_noise():
    toks = tokenize("QuerySet.bulk_update() fails for the HTTPResponse in get_queryset")
    assert "queryset" in toks and "query" in toks and "set" not in toks  # "set" is a stop word
    assert "bulk_update" in toks and "bulk" in toks and "update" in toks
    assert "httpresponse" in toks and "http" in toks and "response" in toks
    assert "the" not in toks and "for" not in toks


def test_bm25_ranks_the_matching_document_first_and_accepts_new_documents():
    idx = BM25()
    idx.add(tokenize("def bulk_update(self, objs, fields): update many rows"))
    idx.add(tokenize("class Paginator: split a list into pages"))
    assert idx.scores(tokenize("bulk update rows")).argmax() == 0
    assert idx.scores(tokenize("paginator pages")).argmax() == 1
    assert idx.scores(tokenize("unrelated words")).max() == 0
    new = idx.add(tokenize("def migrate(): run schema migrations"))
    assert idx.scores(tokenize("schema migrations")).argmax() == new


FILES = [
    {"id": 1, "path": "django/db/models/query.py", "module": "django.db.models.query"},
    {"id": 2, "path": "django/db/models/sql/query.py", "module": "django.db.models.sql.query"},
    {"id": 3, "path": "django/core/paginator.py", "module": "django.core.paginator"},
]
SYMBOLS = [
    {"id": 10, "file_id": 1, "name": "QuerySet", "qualname": "QuerySet", "kind": "class"},
    {"id": 11, "file_id": 1, "name": "bulk_update", "qualname": "QuerySet.bulk_update", "kind": "method"},
    {"id": 12, "file_id": 2, "name": "Query", "qualname": "Query", "kind": "class"},
    {"id": 13, "file_id": 3, "name": "Paginator", "qualname": "Paginator", "kind": "class"},
]


def test_mentions_link_paths_modules_qualnames_and_identifiers():
    linker = MentionLinker(FILES, SYMBOLS)

    m = linker.link('File "/usr/lib/python3/site-packages/django/core/paginator.py", line 12')
    assert set(m.files) == {3}

    # an ambiguous bare file name matches both query.py files, each at reduced strength
    m = linker.link("see query.py")
    assert set(m.files) == {1, 2} and all(v < 1 for v in m.files.values())

    m = linker.link("django.db.models.sql.query raises here")
    assert m.files[2] == 1.0 and 1 not in m.files

    m = linker.link("QuerySet.bulk_update() ignores F expressions")
    assert 1 in m.files and 11 in m.symbols
    assert "QuerySet.bulk_update" in m.evidence[1]

    # plain English words are not treated as code mentions
    assert linker.link("the query is slow when paginating").files == {}
