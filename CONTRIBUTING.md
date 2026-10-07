# Contributing

Code contributions are distributed under the [MIT License](LICENSE). Preserve applicable copyright and license notices when incorporating third-party code. The code license does not cover paper figures or other non-code assets unless separately stated.

Install the locked development environment using the README and run `.venv/bin/pytest` before submitting changes. Tests block accidental network access. Opt-in runtime tests use fixed policies and locally built Docker images.

Preserve the role boundaries and candidate-retention rule. Changes to trajectories, capture settings, benchmark versions or scoring must be explicitly configured and documented. Keep screenshots, recordings, credentials and test outputs outside game source trees. Use relative paths in persisted portable records; resolve host paths only when executing tools.

Add regression coverage for changes to recovery, anonymous evidence, source isolation, scheduling and export. Do not report a skipped integration test as a pass or a mock run as a measured game result.
