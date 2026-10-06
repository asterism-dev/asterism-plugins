# asterism-plugins

The official plugin store for [asterism](https://github.com/asterism-dev/asterism). It is preconfigured in asterism as `asterism-dev`.

`store.json` lists the plugins. Each entry either points to a folder in this repo (`path`) or to another git repo pinned to a tag or full commit SHA (`git`, `ref`, optional `path`). Plugins must run without a build step.
