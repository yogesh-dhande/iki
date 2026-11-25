# Lab roles

`iki/lab_roles` declares five project qualifications. A project admin can grant them to people or teams. A consuming plugin uses the same qualified name in its states, assignments or notifications.

| Role                                      | What the holder does                                     |
| ----------------------------------------- | -------------------------------------------------------- |
| `iki/lab_roles.coordinator`               | Receives material and coordinates requests.              |
| `iki/lab_roles.engineer`                  | Services, calibrates and retires instruments.            |
| `iki/lab_roles.analyst`                   | Performs tests and submits measurements.                 |
| `iki/lab_roles.reviewer`                  | Checks the submitted evidence and signs the review.      |
| `iki/lab_roles.qa` (Sample and result QA) | Signs release of independently reviewed QC deliverables. |

For a delivery, grant the release role in the project doing the work. The workflow still checks project permissions and independence from the people who performed and reviewed the work. Lot disposition uses the separately declared `iki/lots.qa` qualification. [Lab role grants](../../docs/adr/0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)
