# Raneen knowledge ingestion

Status: RAN-KNOW-01 foundation  
Branch: `work/knowledge-ingestion-foundation`

## Purpose

Raneen needs to learn from hundreds of real-estate brochures, PowerPoints, spreadsheets,
floor plans, maps and internal training documents without turning Google Drive into a
runtime database or copying a multi-GB corpus onto the small staging disk.

The source repository remains evidence. Raneen stores a normalized, queryable projection
with exact provenance.

```
Google Drive / later S3
        |
        | immutable source reference
        v
document + version registry
        |
        +--> native extraction
        |
        +--> page / slide render
        |        |
        |        v
        |    visual interpretation
        |
        v
page / slide / sheet / section units
        |
        +--> structured facts
        +--> semantic/lexical chunks
        +--> visual asset references
        |
        v
KnowledgeProvider
```

## Storage rule

The 7.5+ GB source corpus must not be copied into the current 1 GB Render persistent disk.

The MVP stores:

- source IDs and URLs
- version metadata
- extracted text
- small structured JSON
- page/slide/sheet provenance
- visual-asset locators and summaries
- normalized entities and facts

Original PDF/PPTX/XLSX/images stay in the external source repository.

A later production move can use object storage plus Postgres/pgvector without changing the
`KnowledgeProvider` contract.

## Why document versions are first-class

A Drive file is a logical document. Each materially different revision is an immutable
document version.

```
Drive file: "Project X Price List.xlsx"
          |
          +-- version A  -> superseded
          |
          +-- version B  -> current
```

Registering a new version automatically marks facts extracted from the prior version of
that exact file as superseded. Old evidence remains inspectable.

Different files do not silently supersede one another. If an official brochure and a
later sales deck disagree at the same authority level, both facts remain active and
`get_current_fact()` reports a conflict until a later ingestion or human review explicitly
supersedes one.

## Multimodal extraction contract

Every source should be processed in two passes where applicable.

### Pass A — native structure

PPTX:
- slide text
- tables
- notes when relevant
- embedded object metadata

PDF:
- native text
- page order
- tables when extractable

XLSX:
- sheet names
- bounded used ranges
- typed cell values
- table-like regions

### Pass B — rendered visual page/slide

Render the complete page or slide and classify important visual content.

Supported initial asset classes:

- `decorative`
- `property_render`
- `floor_plan`
- `location_map`
- `master_plan`
- `payment_plan`
- `pricing_table`
- `chart`
- `amenity`
- `other`

The model may summarize visible evidence. It must not turn an architectural rendering into
a contractual fact. Example: a visible pool may support `amenity=pool`; it does not support
an exact pool size unless the source states one.

## Normalized information layers

### Documents

Logical source objects identified by provider + external ID.

### Document versions

Immutable versions identified by a source-specific version key such as Drive modified time,
revision ID or stable content hash.

### Units

Page, slide, sheet, section, table or image units. Unit numbering is zero-based internally.
Human-facing page/slide numbers should also be preserved in metadata when they differ.

### Entities

Initial entity classes:

- developer
- project
- community
- property
- unit type
- policy
- market
- agency
- document subject

### Facts

A structured claim tied to an entity and exact source evidence.

Example:

```json
{
  "entity": "Project X",
  "field_key": "payment_plan",
  "value": {
    "booking": 20,
    "construction": 40,
    "handover": 40
  },
  "authority_rank": 90,
  "confidence": 0.99,
  "source": "launch-deck.pptx",
  "slide": 17
}
```

Authority and confidence are separate.

- authority = how authoritative the source class is
- confidence = how confident extraction is that the source actually says the fact

Do not use confidence as a substitute for source authority.

A recommended initial authority policy for real-estate facts is:

```
100  verified live inventory/API
 95  dated official developer price/inventory sheet
 90  official developer update / launch deck
 85  official developer brochure
 75  agency-maintained factual material
 60  internal training material
 40  market commentary
```

This policy belongs in ingestion configuration, not hard-coded conversational prompts.

### Chunks

Retrieval text tied to a document version and usually a unit. The first implementation uses
simple lexical retrieval as a safe infrastructure baseline. Vector and reranking layers are
deliberately not baked into the schema.

### Visual assets

References to meaningful images or regions. The database stores a locator and interpretation,
not the large binary source image.

Examples:

```
slide:17#payment-plan
page:31#floor-plan-A
sheet:Inventory!A1:M82
```

## Provenance invariant

Every factual answer used by Raneen must be able to resolve:

1. logical source file
2. exact immutable source version
3. page / slide / sheet / section when available
4. extraction confidence
5. source authority
6. active / superseded state

If this chain is unavailable, the value is not a verified Raneen fact.

## Runtime rule

Do not query Google Drive during a customer conversation.

```
wrong:
caller -> search Drive -> download files -> LLM -> answer

right:
Drive change -> ingestion -> Raneen knowledge store
caller -> KnowledgeProvider -> bounded evidence -> answer
```

This prevents latency, stale ranking, repeated parsing cost and uncontrolled context growth.

## Current API foundation

Admin-only endpoints:

```
GET   /api/knowledge/status

POST  /api/knowledge/ingestions
PATCH /api/knowledge/ingestions/{id}

POST  /api/knowledge/documents
GET   /api/knowledge/documents

POST  /api/knowledge/units
POST  /api/knowledge/entities
GET   /api/knowledge/entities/{id}

POST  /api/knowledge/facts
GET   /api/knowledge/entities/{id}/facts/{field}
GET   /api/knowledge/facts/{id}/provenance

POST  /api/knowledge/chunks
POST  /api/knowledge/assets
POST  /api/knowledge/relations

GET   /api/knowledge/search?q=...
```

Each write is small and idempotent where an ingestion worker is likely to retry it. The
existing API request-size guard remains in place; a 400-document corpus is streamed as
records rather than posted as one enormous JSON body.

## What happens when the Drive upload finishes

Phase 1 — corpus census:
- enumerate all files/folders
- MIME/type counts
- size distribution
- modified dates
- likely developer/project names
- obvious duplicate names
- PowerPoint/PDF/Excel/image counts
- unusually large or unreadable items

Phase 2 — stratification:
- high-value commercial facts
- core project knowledge
- sales intelligence
- visual reference
- low-value/duplicate/obsolete candidates

Phase 3 — extraction:
- native text/table extraction
- rendered-page/slide visual pass
- entities
- facts
- chunks
- assets
- provenance

Phase 4 — review:
- conflicts
- likely superseded material
- low-confidence extraction
- ambiguous project/developer identity

Phase 5 — retrieval evaluation:
- factual queries
- conflicting-version queries
- Arabic/English aliases
- project-name disambiguation
- payment-plan/price/handover tests

Only after this corpus audit should we choose the vector model and whether Postgres/pgvector
is justified immediately.

## Google Drive connector

ChatGPT's connected Drive can be used for the manual corpus audit and classification work.
Raneen's runtime must not depend on that ChatGPT connection.

For automated recurring synchronization, the next connector milestone should use a dedicated
Google identity with least-privilege access to only the Raneen source folder. The source folder
can then be polled by modification/revision cursor and fed into the same ingestion contract.

No Drive secret or OAuth token belongs in Git.
