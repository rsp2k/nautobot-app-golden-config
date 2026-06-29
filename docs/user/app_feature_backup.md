# Configuration Backup

The backup configuration process requires the Nautobot worker to connect via Nornir to the device, and run the `show run` or equivalent command, 
and save the configuration. The high-level process to run backups is:

* Download the latest version of each of the Git repositories configured with the `backup configs` capability within Nautobot.
* Run a Nornir play to obtain the cli configurations.
* Optionally perform some lightweight processing of the backup.
* Store each device's backup configuration file on the local filesystem.
* Commit all files added or changed in each repository.
* Push configuration files to the remote Git repositories.

## Configuration Backup Settings

Backup configurations often need some amount of parsing to stay sane. The two obvious use cases are firstly the ability to remove lines such as the "Last 
Configuration" changed date, as this will cause unnecessary changes and secondly stripping out secrets from the configuration. In an effort to support these use cases, the following settings are available and further documented below.

* Config Removals - provides the ability to remove a line based on a regex match.
* Config Replacements - provides the ability to swap out parts of a line based on a regex match.

### Backup Repositories

In the `Backup Repository` field of the Settings, configure the repository which you intend to use for backed-up device configurations as part of Golden Config.

Backup repositories must first be configured under **Extensibility -> Git Repositories**. When you configure a repository, look for the `Provides` field in the UI. To serve as a configuration backup store, the repository must be configured with the `backup configs` capability under the `Provides` field. For further details, refer to [Navigating Nautobot Git Settings](./app_use_cases.md#git-settings).


### Backup Path Template

The `backup_path_template` setting gives you a way to dynamically place each device's configuration file in the repository file structure. This setting uses the GraphQL query configured for the app. It works in a similar way to the Backup Repository Matching Rule above. Since the setting uses a GraphQL query, any valid Device model method is available. The app renders the values from the query, using Jinja2, to the relative path and file name in which to store a given device's configuration inside its backup repository. This may seem complicated, but the equivalent of `obj` by example would be:

```python
obj = Device.objects.get(name="nyc-rt01")
```

An example would be:
```python
backup_path_template = "{{obj.location.name|slugify}}/{{obj.name}}.cfg"
```

With a Sydney, AU device `SYD001AURTR32`, in the location named `Sydney001` and the GraphQL query and `backup_path_template` configured above, our backed-up config would be placed in the repo in `/sydney001/SYD001AURTR32.cfg`.

The backup process will automatically create folders as required based on the path definition. 

The `backup_path_template` can be set in the UI.  For navigation details [see](./app_use_cases.md#application-settings).

### Device Login Credentials

The credentials/secrets management occurs within the [nautobot-plugin-nornir](https://github.com/nautobot/nautobot-plugin-nornir) library and is described in the [Navigating Credentials](https://docs.nautobot.com/projects/plugin-nornir/en/latest/user/app_feature_credentials/) documentation. For the simplest use case you can set environment variables for `NAPALM_USERNAME`, `NAPALM_PASSWORD`, and `DEVICE_SECRET` in conjunction with the `credentials` string shown below in your configuration for `nautobot-plugin-nornir`.

```python
PLUGINS_CONFIG = {
    "nautobot_plugin_nornir": {
        "nornir_settings": {
           "credentials": "nautobot_plugin_nornir.plugins.credentials.env_vars.CredentialsEnvVars"
        },
    }
}
```

## Starting a Backup Job

To start a backup job manually:

1. Navigate to the App Home (Golden Config->Home), with Home being in the `Golden Configuration` section
2. Select _Execute_ on the upper right buttons, then _Backup_
3. Fill in the data that you wish to have backed up
4. Select _Run Job_

## Config Removals

The line removals settings is a series of regex patterns to identify lines that should be removed. This is helpful as there are usually parts of the
configurations that will change each time. A match simply means to remove.

In order to specify line removals. Navigate to **Golden Config -> Config Removals**.  Click the **Add** button and fill out the details.

The remove setting is based on `Platform`.  An example is shown below.
![Config Removals View](../images/ss1_00-navigating-backup_light.png#only-light){ .on-glb }
![Config Removals View](../images/ss1_00-navigating-backup_dark.png#only-dark){ .on-glb }

## Config Replacements

This is a replacement config with a regex pattern with a single capture groups to replace. This is helpful to strip out secrets.

The replace lines setting is based on `Platform`.  An example is shown below.

![Config Replacements View](../images/ss1_01-navigating-backup_light.png#only-light){ .on-glb }
![Config Replacements View](../images/ss1_01-navigating-backup_dark.png#only-dark){ .on-glb }

The line replace uses Python's `re.sub` method. As shown, a common pattern is to obtain the non-confidential data in a capture group e.g. `()`, and return the rest of the string returned in the backreference, e.g. `\2`.

```python
re.sub(r"(username\s+\S+\spassword\s+5\s+)\S+(\s+role\s+\S+)", r"\1<redacted_config>\2", config, flags=re.MULTILINE))
```

## Rancid-Style Backups (per-device commits)

By default the backup job creates a single git commit per job run, containing every
device's config, authored by the automation user and stamped with the job run time.
History lives in the backup repository, keyed by the rendered `backup_path_template`
(typically a hostname/location path).

Rancid-style backups change this so that history tracks the *physical device* the way
a rancid archive does:

| Axis | Default | Rancid-style |
| --- | --- | --- |
| Commit cadence | one per job run (all devices) | one per device per snapshot |
| Commit author | automation user | the engineer named on the config's `Last configuration change` line |
| Commit timestamp | job run time | the config's last-change time, falling back to the backup time |
| File identity | rendered `backup_path_template` | the device, tracked by its Nautobot UUID |
| Hostname renames | new file appears, old one orphaned | recorded as a git rename, so `git log --follow` traces the device |

Device identity is the stable Nautobot Device UUID, so a hostname rename (which changes
the rendered `backup_path_template`) is detected and committed as a `git mv`. The
UUID-to-path mapping is stored in a `.golden-rancid-manifest.json` file inside the
backup repository, so the feature keeps no state outside the repo.

The resulting history is one commit per device per snapshot, each authored by the engineer
named on the device's config and dated to the change time, with `git log --follow` tracing
a device through a hostname rename:

![Rancid-style git history](../images/ss1_rancid-history_light.png#only-light){ .on-glb }
![Rancid-style git history](../images/ss1_rancid-history_dark.png#only-dark){ .on-glb }

### Enabling

Set `enable_rancid_backup` to `True` in the app settings (see the
[install guide](../admin/install.md#app-configuration)). It is disabled by default;
when off, the standard single-commit-per-run behavior is used unchanged. Only the
backup repository is affected — intended and compliance repositories always use the
standard commit behavior.

```python
PLUGINS_CONFIG = {
    "nautobot_golden_config": {
        "enable_rancid_backup": True,
        "rancid_email_domain": "example.com",  # builds <user>@<domain> for parsed authors
    }
}
```

### Author and timestamp parsing

The author and original change time come from the device config's
`! Last configuration change at <time> by <user>` line. When that line is absent the
commit falls back to the backup time and the backup repository's configured git
identity, so backups always succeed even without attribution.

!!! warning
    If a `Config Removal` regex strips the `Last configuration change` line (a common
    pattern, since the timestamp changes on every backup and otherwise causes noisy
    diffs), the per-engineer author and original timestamp cannot be recovered, and
    commits fall back to the backup time and automation identity. To keep attribution,
    leave that line in the backup.

### Example queries

```bash
# Full history of one device, traced through any hostname renames
git log --follow devices/<name>.cfg

# Everything an engineer changed across the fleet
git log --author=jdoe

# What changed during a maintenance window
git log --since='2025-06-01' --until='2025-06-15'
```
