PAP Pilot Query List
====================

PURPOSE AND BASIS
This pilot supports Google AI Overview auditing across U.S. policy domains.
It uses the U.S. Policy Agendas Project (PAP) Topics Codebook, Updated Spring
2019, as a classification framework.

Five information needs were purposively selected in each of 20 PAP major
domains. Each need has three English wording variants: 100 families and
300 unique queries. Three collection windows would yield 900 observations.

PAP supplies the categories and coding rules, not the query text. The topics
and queries were constructed with AI assistance and reviewed during generation.
Independent human review is still pending. This is not a random sample of
public searches and does not cover every PAP subtopic. Wikipedia, Google Trends,
GDELT, and observed AI Overview results were not used to select or weight it.

Source:
https://minio.la.utexas.edu/compagendas/codebookfiles/Codebook_PAP_2019.pdf

FILES
pap_pilot_queries.csv: One row per query variant, with all columns below.
pap_pilot_queries.xlsx:
  Queries: Query text, IDs, main classification fields, and review status.
  Topics: One row per information need, including rationale and source mapping.
  Methods: Design choices, limitations, and suggested collection procedures.

CSV COLUMN DEFINITIONS (in file order)
query_id
  Unique ID for an exact query variant, e.g. PAP01-01-V1.

query
  Exact English text to submit to the search engine.

family_id
  ID for the shared information need. Its three variants share this ID.
  PAP01-01 means major domain 1, first selected information need.
  The final two digits are our family number, not a PAP subtopic code.

variant_id
  V1, V2, or V3 within the family.

variant_type
  keyword_phrase: Keyword-style phrase; may retain "how" to preserve intent.
  direct_question: Direct question.
  paraphrased_question: Alternative question wording for the same need.

pap_major_code
  Original PAP major policy-domain code.

pap_major_label
  English name of that PAP major domain.

pap_subtopic_code
  Primary PAP subtopic assigned to this information need.

pap_subtopic_label
  English name of that PAP subtopic.

topic
  Researcher-defined topic label; more specific than the PAP category.

intent
  Type of information requested, such as policy_effects, legal_framework,
  policy_mechanism, eligibility, or funding_mechanism. Fixed within a family.

invariant_information_need
  The intended meaning shared by all three variants. Use this to check that
  changes in wording do not change the question being asked.

selection_rationale
  Reason for including this information need in the purposive pilot.

mapping_note
  Coding qualifications, related categories, or policy-specific cautions.
  Blank means no additional mapping note was recorded.

language
  Query language. "en" means English.

geographic_scope
  Substantive focus: United States. This is not the search location setting.

framing
  "non_directional" means the wording avoids asserting a positive or negative
  policy effect. It does not establish political neutrality.

list_version
  Internal identifier for the query-list revision; not the PAP codebook version.

created_date
  Date the query-list record was created, in YYYY-MM-DD format.

review_status
  "draft_for_independent_review" means independent human review is pending.

source_pdf_page
  One-based PDF viewer page containing the assigned PAP subtopic heading.

source_printed_page
  Page number printed on the corresponding codebook page.

source_url
  Codebook link with a page anchor for checking the PAP classification.
  It supports the category mapping, not an answer to the query.

NOTES FOR USE
Keep query_id and family_id in collected results. The 300 strings belong to
100 information-need families; repeated observations are not independent topics.
Review and freeze the query text before collection. Retain searches without
an AI Overview and distinguish them from collection failures.
