# devstack UI Benchmark Notes

## Goal

devstack is not a deployment tool. The benchmark target is the operational UI pattern shared by deployment and container tools: make current state, execution progress, logs, and recovery actions visible in one loop.

## Sources

- Argo CD: application views expose sync status, health status, resource components, logs, events, and a manual sync action.
  Source: https://github.com/argoproj/argo-cd/blob/master/docs/getting_started.md
- CircleCI: the pipelines dashboard uses filters, workflow/job maps, individual job steps, and quick controls such as rerun and cancel.
  Source: https://circleci.com/docs/guides/orchestrate/pipelines/
- GitHub Actions: workflow runs provide a real-time graph, run logs, job status, and job execution time.
  Source: https://docs.github.com/en/actions/how-tos/monitor-workflows
- GitLab CI/CD: pipelines are organized as stages and jobs, can be manually run, and model dependencies between jobs or stages.
  Source: https://docs.gitlab.com/ci/pipelines/
- Jenkins Blue Ocean: dashboard lists pipelines with health and run status indicators, plus focused navigation to run details.
  Source: https://www.jenkins.io/doc/book/blueocean/dashboard/
- Portainer: dashboard uses summary tiles for services/containers and service logs include search, copy, line count, wrap, timestamps, and refresh controls.
  Sources: https://docs.portainer.io/2.27/user/docker/dashboard and https://docs.portainer.io/user/docker/services/logs
- Lens: workload logs are selected from a workload/pod context and can switch between containers.
  Source: https://docs.k8slens.dev/cluster/view-logs/

## Patterns Adapted For devstack

- Dashboard summary: keep workspace, target count, and service count visible.
- Pipeline rhythm: show a compact local flow of Select -> Start -> Observe -> Recover instead of deploy stages.
- Service board: list local services with role, type, port, selection, and status feedback from `devstack status`.
- Log-first operations: keep the fixed log/output pane as the primary work surface.
- Recovery loop: make AI recovery prompt generation a first-class action next to logs and restart.
- Safe controls: keep destructive workspace actions in advanced controls and preserve explicit confirmation.

## Deliberately Not Adopted

- Deploy/release language, because devstack starts local development servers.
- Commit, branch, artifact, approval, or production rollout concepts.
- Large graph visualizations, because devstack currently has lightweight local dependency data rather than a full deployment graph.
