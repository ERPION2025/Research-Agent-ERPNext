"""Add a MariaDB FULLTEXT index on Document Chunk.chunk_text.

keyword_search() in agent/rag/retrieve.py runs MATCH() AGAINST() over this
column. That syntax requires a FULLTEXT index; without one, MariaDB refuses
the query, and keyword_search catches the exception and returns an empty
list rather than raising. A site that never has this index gets dense-only
retrieval permanently, with no error the reader would ever see. That is
exactly the class of silent degradation this app is built to prevent
everywhere else, so it should not be permitted here either.

Frappe's doctype JSON has no field-level FULLTEXT option -- search_index
gives a plain BTREE index, not a text index -- so this has to be a raw DDL
patch. Guarded to be a no-op on a second run, since patches can replay.
"""

import frappe


def execute():
	if "Document Chunk" not in frappe.get_all("DocType", pluck="name"):
		return  # RAG layer not installed on this site yet; nothing to index

	table = "tabDocument Chunk"
	existing = frappe.db.sql(
		"""
		select index_name from information_schema.statistics
		where table_schema = database() and table_name = %s and index_type = 'FULLTEXT'
		""",
		(table,),
	)
	if existing:
		return

	frappe.db.sql(f"alter table `{table}` add fulltext index ft_chunk_text (chunk_text)")
	frappe.db.commit()
	print("Research Agent: added FULLTEXT index on Document Chunk.chunk_text")
