# Repository Guidelines

## Project Structure & Module Organization

This is a Chinese-language storage systems course with self-contained lessons and local exercises. `modules/M00`–`modules/M15` holds lesson Markdown (`Mxx-Lnn-topic.md`). `labs/lab-NN-topic/` contains exercise READMEs, source, and tests. `projects/MiniStore/` and `projects/IndexCacheLab/` are standalone C++ projects. Course plans/maps are in `课程大纲/`, lesson templates in `templates/`, diagrams/data in `assets/`, and source material in `references/`.

## Build, Test, and Development Commands

There is no root build or test command. Run checks from the component you changed:

- `make -C projects/MiniStore test` builds MiniStore and runs its Python black-box suite; `make -C projects/MiniStore ci` cleans, rebuilds, and runs the suite.
- `make -C projects/IndexCacheLab test` builds and runs its C++ tests and report check.
- Makefile-based labs support `make -C labs/lab-NN-topic test` (for example, `labs/lab-01-block-image`).
- Python labs use `cd labs/lab-NN-topic && python3 -m unittest -v test_<topic>.py`.

Consult the component README for optional dependencies, exact test names, and safety limits before running an exercise.

## Coding Style & Naming Conventions

Follow nearby code and README examples. C labs use C11; C++ projects use C++20. Existing Makefiles enable `-Wall -Wextra -Wpedantic -Wconversion -Wshadow`; keep code warning-clean and use the local Makefile. Python exercises use standard-library `unittest`, snake_case modules, and `test_*.py` tests. Name lessons `Mxx-Lnn-主题.md` and labs `lab-NN-kebab-case`.

## Testing Guidelines

Run the narrow test target for each changed lab or project; check lesson instructions when changing docs. Tests should be deterministic and use documented temporary paths. Do not present local model results as hardware, production safety, or external-stack validation.

## Commit & Pull Request Guidelines

Recent commits use short subjects with a lowercase type prefix, such as `docs: ...` and `feat: ...`; use `fix:` for corrections. PRs should explain the change, list affected modules/labs, report checks run, and state unverified dependencies or platform limits. Link a related issue when available; include screenshots for visual changes when useful.

## Safety & Configuration

Each exercise README defines its safety boundary. Preserve temporary-path and ordinary-file safeguards. Do not direct experiments at `/dev`, user data, cloud services, or real clusters unless that exercise documents a safe optional setup. Record tested versions and distinguish local tests from integration validation.
