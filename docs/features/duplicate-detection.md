# Feature: Duplicate Detection Before Upload

**Value:** High
**Effort:** Low
**Touches:** `knowledger/claude_client.py`, `knowledger/bot.py`

## Problem

Sending the same YouTube URL twice (or sending a URL already uploaded via the manual workflow) silently creates a duplicate document in the Claude knowledge base. Duplicate documents degrade retrieval quality because Claude sees the same content twice, inflating its apparent relevance. There is currently no way to detect or prevent this.

## Proposed Solution

Before uploading, check whether a document with the same filename already exists in the target project. If a duplicate is found, present the user with a choice: skip the upload, or overwrite (delete the old document and upload the new one).

## Implementation

### 1. Extend `ClaudeClient`

Add two methods:

`list_docs(project_id) -> list[dict]`
GET `/organizations/{org_id}/projects/{project_id}/docs`
Returns the list of documents already in the project. Each entry contains at least `uuid` and `file_name`.

`delete_doc(project_id, doc_uuid)`
DELETE `/organizations/{org_id}/projects/{project_id}/docs/{doc_uuid}`
Removes an existing document, enabling an overwrite flow.

### 2. Update `handle_project_selection` in `bot.py`

After building `file_name` and before uploading:

1. Call `list_docs(project_id)` to get existing documents.
2. Search for a document whose `file_name` matches the one about to be uploaded.
3. If no match: proceed with the existing upload flow unchanged.
4. If a match is found: edit the message to notify the user and present two inline buttons — **Skip** and **Overwrite** — encoded as new callback data (e.g. `skip:{msg_id}` and `overwrite:{doc_uuid}:{msg_id}`).

A new `handle_duplicate_choice` callback query handler processes the user's response:

- **Skip**: edit message to "Already in project — skipped." and clean up `user_data`.
- **Overwrite**: call `delete_doc`, then `upload_content`, then confirm success.

### 3. No new dependencies

All logic uses the existing `ClaudeClient` session and `user_data` session storage already in place.

## User Experience

For new videos the flow is identical to today — no added friction.
For duplicates the user sees:

> ⚠️ *Youtube - ChannelName - VideoTitle* already exists in this project.
> [Skip] [Overwrite]

The choice is preserved in `user_data` alongside the existing `video_{msg_id}` entry so it survives until the user acts.

## Why This Produces the Most Value

Knowledge base pollution is invisible to the user and silently worsens every future Claude session. This fix is a single API call added to an already-existing flow, with no new dependencies and minimal added complexity.
