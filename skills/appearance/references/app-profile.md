# App profile branches

## Photo and description

The public app-definition API accepts `avatarUrl`, `characterConfig` and
`description`. It requires the human app owner's API key; an installation token
cannot edit its own app definition. When that management connection is absent,
prepare the files/text and guide the human through Settings → Agents & Apps.
Never request a credential in chat to finish an appearance draft.

The avatar input is a public PNG, JPEG, WebP or GIF URL, at most 2 MB. SoWork stores
its own copy. No binary avatar-upload endpoint or mandatory pixel size was found
in the API 1.36.0 research. A centered square 512×512 PNG/WebP is a preparation
recommendation, not an official size requirement. Public hosting and changing the
app are separate actions; do not publish a private portrait merely to obtain a URL.
A description is up to 1,024 characters and should describe enabled capabilities.
Public app metadata is not the Hermes persona/system prompt.

## Character identity and NPC

`characterConfig` is the definition's character. `avatarUrl` takes precedence in
portrait contexts when both exist. An office NPC is a different resource.
An installation token with `npcs.write` can control its owned NPC; resolve it via
`GET /v1/app` → `installation.npcIds` (usually `main`), not a guessed owner/name.
Definition-to-existing-NPC appearance propagation was not verified. Verify each
resource separately after any explicitly authorized write. For a requested change,
review a minimal patch; a full-array patch replaces the existing array.

## Permissions, events and delivery

Select scopes for the feature actually implemented. The basic DM bridge uses
`chat.dm`, `chat.write` and `events.subscribe:chat.direct_message_created`.
Threads additionally use `chat.history` and the thread-reply event scope. Explicit
channel/mention routing has separate configuration; follow the current README.
`chat.dm.history` concerns delegated human DM access, not the bot's own DM.
`act_on_behalf` changes whose access/identity is used, within granted scopes; it
is not required for an ordinary bot conversation. NPC permissions are separate
from app-definition administration. Granted scopes are checked in `GET /v1/app`;
adding a definition scope does not automatically grant it to an existing install.

Event subscriptions specify what reaches a receiver. The bridge consumes an
exclusive inbox subscription using poll/SSE and a saved cursor; no webhook URL is
needed for this path. A webhook URL is a public HTTPS receiver for signed POSTs.
Changing an existing app webhook may immediately affect existing installations;
preserve unrelated subscriptions and consumers. Neither a new scope nor selecting
an event implements a missing handler in the plugin.

## Sources and uncertainty

Public contracts researched on 2026-10-21 (API 1.36.0); verify updated docs before
administrative writes. The local helpers fetch frontend catalog files on demand.

- [Apps and consent](https://api.sowork.com/public/documentation/apps.md)
- [App definitions](https://api.sowork.com/public/documentation/reference/appdefinitions.md)
- [OpenAPI](https://api.sowork.com/public/documentation/json)
- [NPCs](https://api.sowork.com/public/documentation/reference/npcs.md)
- [Subscriptions](https://api.sowork.com/public/documentation/reference/subscriptions.md)
- [API changelog](https://api.sowork.com/public/documentation/changelog.md)
