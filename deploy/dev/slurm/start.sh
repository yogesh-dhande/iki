#!/usr/bin/env bash
# Starts the test cluster's daemons in order, sets up its accounts, and waits.
# Prints "slurm ready" once a job can be submitted.
set -euo pipefail

wait_for() {
  local what=$1
  shift
  for _ in $(seq 1 60); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "$what didn't start" >&2
  tail -n 50 /var/log/slurm/*.log >&2 || true
  exit 1
}

# The node, as this container is: every Slurm command reads it with slurm.conf.
slurmd -C | head -n 1 | sed 's/$/ State=UNKNOWN/' >/etc/slurm/nodes.conf

# munge authenticates every Slurm daemon and command.
install -d -o munge -g munge -m 0755 /run/munge
runuser -u munge -- /usr/sbin/munged
wait_for munged munge -n

# Accounting's database.
install -d -o mysql -g mysql /run/mysqld
mysqld_safe --skip-networking=0 --bind-address=127.0.0.1 >/dev/null 2>&1 &
wait_for mariadb mysqladmin ping
mysql -e "CREATE DATABASE IF NOT EXISTS slurm_acct_db;
  CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY 'slurm';
  GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';"
/usr/sbin/slurmdbd
wait_for slurmdbd sacctmgr -n list cluster

# The cluster, and the accounts Iki's service account may charge.
sacctmgr -i add cluster hpc1 >/dev/null
sacctmgr -i add account rd-shared,rd-protein Cluster=hpc1 >/dev/null
sacctmgr -i add user iki Account=rd-shared,rd-protein DefaultAccount=rd-shared >/dev/null

/usr/sbin/slurmctld
wait_for slurmctld scontrol ping
/usr/sbin/slurmd
wait_for slurmd sh -c 'sinfo -h -N -o %t | grep -Eq "^(idle|mix|alloc)"'

echo "slurm ready"
exec sleep infinity
