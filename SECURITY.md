# Security Policy / 安全政策

## Supported versions

Security fixes target the latest release and the `main` branch. Older releases may be asked to upgrade before a fix is backported.

| Version | Supported |
|---|---|
| Latest release | Yes |
| `main` | Yes |
| Older releases | Best effort |

## Reporting a vulnerability

Do **not** open a public Issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting page:

<https://github.com/Rabbitgenius-rgb/restore-bead-pattern/security/advisories/new>

Include, when possible:

- affected command and version;
- impact and threat model;
- minimal reproduction using synthetic data;
- whether the issue can expose source pixels, local paths, or other private data;
- proposed mitigation, if known.

Reports are handled on a best-effort basis. Please allow the maintainer time to reproduce and coordinate a fix before public disclosure.

The project currently offers no bug bounty. Good-faith research should use your own local checkout and synthetic data, must not access other people's systems or data, and must stop if it risks privacy or data loss. Reporters may request public credit when an advisory is published.

## Security-sensitive areas

Especially useful reports include:

- bypasses of guarded `--overwrite` output ownership;
- path traversal, symlink races, or deletion outside an owned output directory;
- code execution or spreadsheet formula injection through image, palette, CSV, or JSON input;
- decompression bombs or resource-limit bypasses;
- accidental persistence or upload of source images, absolute paths, credentials, or private metadata;
- loss of required third-party license notices in redistributed palette output.

Documented limitations such as provisional color matching, expected `review` results, or overlays intentionally containing source pixels are not vulnerabilities by themselves.
