"""Platform golang smoke command tweaks for Konflux Tekton runs."""


def prepend_platform_smoke_command(run_command: str) -> str:
    """gotestsum writes results/xunit_report.xml relative to /e2e workingDir."""
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    return f"mkdir -p results && {cmd}"
