# Notes for Claude Code

This repo manages a HubSpot portal's CRM setup from `portal.yaml`.

## Hard rules
- Only run `apply`, `seed` or `clean-seed` against a **developer test account**. Never a production or company portal.
- Never print, log or commit the access token. It lives in `.env`, which is git-ignored.
- Always run `python -m portal_as_code plan` and show me the output before running `apply`.
- Don't add delete operations for properties, pipelines or stages. The tool is additive on purpose.

## Workflow
- Change the portal by editing `portal.yaml`, not by calling the API ad hoc.
- After any code change, run `python -m pytest`; all tests must pass.
- New HubSpot endpoints go in `portal_as_code/client.py` (all API paths live there), with a matching method on `tests/fake_hubspot.py`.
- When unsure how a HubSpot API behaves, search the docs with the HubSpotDev MCP server before guessing.

## Commands
- `python -m portal_as_code validate` - check the config locally
- `python -m portal_as_code plan` - preview changes
- `python -m portal_as_code apply` - apply changes (asks for confirmation)
- `python -m portal_as_code seed` - load fake data
- `python -m portal_as_code clean-seed` - archive seeded records
