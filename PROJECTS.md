# Built with VilloMillo

Projects built with or for VilloMillo are listed in
**[Awesome VilloMillo](https://github.com/ColinGPT9/awesome-clips-kitty)**, a curated list of
pipelines, plugins, integrations, apps and tools, in its own repository. It includes
the official ones: the VilloMillo OBS Plugin, the MCP server and agent skill,
and the Python SDK. The VilloMillo Marketplace shows the same list.

VilloMillo runs on your own PC and exposes the same API its app uses. Anything
that can make an HTTP request can queue a video, follow its progress and read
the clips it made. Start with [docs/API.md](docs/API.md).

## Add your project

Add one small YAML file with a pull request.
[CONTRIBUTING.md](https://github.com/ColinGPT9/awesome-clips-kitty/blob/main/CONTRIBUTING.md) has the format and the
inclusion criteria. A separate app or tool that uses the API or the SDK is
"built with VilloMillo"; a pipeline that runs inside VilloMillo is a listing,
installed from the Marketplace
([Marketplace publishing](docs/developers/marketplace-publishing.md)).

Two things matter most:

- **It uses the documented API.** Build on the endpoints in
  [docs/API.md](docs/API.md), not by reading the database or the data folder
  directly, which change without notice.
- **It is upfront about data.** If it sends anyone's videos, clips or accounts
  anywhere, it says so, and its entry says so too.

Add the `clips-kitty` topic to your repository too, so people can find it on
GitHub even before it is listed.
