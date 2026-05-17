# k8sfs

`k8sfs.py` is a small Python FUSE filesystem that presents parts of a Kubernetes cluster as files and directories.

It lets you browse Kubernetes namespaces, Deployments, Pods, and Nodes with normal shell commands such as `ls`, `cat`, `readlink`, and `rm`.

The filesystem is intended as an experimental/admin convenience tool, not a replacement for `kubectl`.

## What it exposes

After mounting, the root directory contains:

- one directory for each Kubernetes namespace
- one directory for each Kubernetes node

Namespace directories contain Deployment directories:

```text
<mountpoint>/
  default/
    nginx/
      deployment.yaml
      nginx-7f456874f4-bx29p
      nginx-7f456874f4-mjj9q
  kube-system/
    coredns/
      deployment.yaml
      coredns-674b8bbfcf-dtx5t
  node0/
    default__nginx-7f456874f4-bx29p -> ../default/nginx/nginx-7f456874f4-bx29p
  node1/
    kube-system__coredns-674b8bbfcf-dtx5t -> ../kube-system/coredns/coredns-674b8bbfcf-dtx5t
```

### Namespace and Deployment view

Each namespace directory contains a subdirectory for each Deployment in that namespace:

```bash
ls ~/mnt/k8s/default
```

Example output:

```text
nginx
web
api
```

Each Deployment directory contains:

- `deployment.yaml` — the current Kubernetes Deployment object serialized as YAML
- one text file per Pod currently selected by that Deployment

```bash
ls ~/mnt/k8s/default/nginx
```

Example output:

```text
deployment.yaml
nginx-7f456874f4-bx29p
nginx-7f456874f4-mjj9q
```

### Deployment YAML file

Read `deployment.yaml` to see the Deployment definition returned by the Kubernetes API:

```bash
cat ~/mnt/k8s/default/nginx/deployment.yaml
```

This is generated from the live Kubernetes API object. It may include server-populated metadata and status fields, so it is not necessarily identical to the original manifest that was applied.

`deployment.yaml` is read-only and cannot be deleted through the filesystem.

### Pod status files

Each Pod appears as a read-only text file containing a compact status summary:

```bash
cat ~/mnt/k8s/default/nginx/nginx-7f456874f4-bx29p
```

Example output:

```text
Ready: 1/1
Status: Running
Restarts: 0
Age: 161m
IP: 10.244.1.113
Node: node1
```

### Deleting Pods

Deleting a Pod file deletes the real Kubernetes Pod:

```bash
rm ~/mnt/k8s/default/nginx/nginx-7f456874f4-bx29p
```

This calls the Kubernetes API equivalent of:

```bash
kubectl delete pod nginx-7f456874f4-bx29p -n default
```

Only real Pod files under a Deployment directory can be deleted. Node symlinks and `deployment.yaml` cannot be deleted through the filesystem.

### Node view

Each node directory contains symlinks to the Pod files for Deployment-owned Pods running on that node:

```bash
ls -l ~/mnt/k8s/node1
```

Example output:

```text
default__nginx-7f456874f4-bx29p -> ../default/nginx/nginx-7f456874f4-bx29p
kube-system__coredns-674b8bbfcf-dtx5t -> ../kube-system/coredns/coredns-674b8bbfcf-dtx5t
```

The namespace is included in the symlink name to avoid collisions when Pods in different namespaces have the same name.

## Requirements

- Python 3
- Access to a Kubernetes cluster
- A working kubeconfig, or in-cluster Kubernetes credentials
- FUSE support on your operating system
- Python packages:
  - `fusepy`
  - `kubernetes`
  - `PyYAML`

## Install

### macOS

Install macFUSE:

```bash
brew install --cask macfuse
```

You may need to approve the macFUSE system extension in macOS System Settings and reboot or log out/in before FUSE mounts work.

Create a Python virtual environment and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fusepy kubernetes PyYAML
```

`k8sfs.py` tries to auto-detect common Homebrew/macFUSE library paths and sets `FUSE_LIBRARY_PATH` automatically when possible.

### Linux

On Debian or Ubuntu, install FUSE and Python tooling:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv fuse libfuse2
```

Then install the Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fusepy kubernetes PyYAML
```

Some distributions use FUSE 3 tools instead. If `fusermount` is not available, try installing your distribution's `fuse3` package and use `fusermount3` when unmounting.

## Kubernetes access

When started, `k8sfs.py` tries to load credentials in this order:

1. local kubeconfig, using `config.load_kube_config()`
2. in-cluster credentials, using `config.load_incluster_config()`

For local use, confirm that `kubectl` can reach the target cluster first:

```bash
kubectl get namespaces
kubectl get nodes
kubectl get deployments --all-namespaces
```

The user or ServiceAccount running `k8sfs.py` needs permission to:

- list namespaces
- list nodes
- list Pods across namespaces
- get and list Deployments
- get ReplicaSets
- delete Pods, if you want `rm <pod-file>` to work

Example ClusterRole for full functionality:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8sfs
rules:
  - apiGroups: [""]
    resources: ["namespaces", "nodes"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list"]
```

Bind it to a user, group, or ServiceAccount as appropriate for your cluster.

## Usage

Create a mountpoint:

```bash
mkdir -p ~/mnt/k8s
```

Run the filesystem in the foreground:

```bash
python3 k8sfs.py ~/mnt/k8s
```

Use another terminal to browse it:

```bash
ls ~/mnt/k8s
ls ~/mnt/k8s/default
ls ~/mnt/k8s/default/nginx
cat ~/mnt/k8s/default/nginx/deployment.yaml
cat ~/mnt/k8s/default/nginx/nginx-7f456874f4-bx29p
ls -l ~/mnt/k8s/node1
```

Stop the filesystem with `Ctrl-C` in the terminal running `k8sfs.py`.

## Unmounting

On macOS:

```bash
umount ~/mnt/k8s
```

or:

```bash
diskutil unmount ~/mnt/k8s
```

On Linux:

```bash
fusermount -u ~/mnt/k8s
```

or, on FUSE 3 systems:

```bash
fusermount3 -u ~/mnt/k8s
```

If the mount is busy, close shells or programs that have files open inside the mountpoint, then retry.

## Behaviour and limitations

- The filesystem is mostly read-only.
- The only write-like operation is deleting a Pod file, which deletes the Kubernetes Pod.
- `deployment.yaml` is generated from the live Deployment object returned by the Kubernetes API.
- Pod files are generated dynamically from Pod status.
- The cache TTL is 5 seconds, so recent Kubernetes changes may take a few seconds to appear.
- Deployment directories show Pods selected by the Deployment's label selector.
- Node directories only link to Deployment-owned Pods. Standalone Pods, DaemonSet Pods, Job Pods, and other non-Deployment Pods are not represented in the namespace/deployment layout.
- The filesystem runs in the foreground and uses `nothreads=True`.
- It does not expose logs, ConfigMaps, Secrets, Services, StatefulSets, DaemonSets, Jobs, or Events.

## Troubleshooting

### `Unable to find libfuse` or similar macOS errors

Confirm macFUSE is installed and approved in System Settings. If needed, set `FUSE_LIBRARY_PATH` manually:

```bash
export FUSE_LIBRARY_PATH=/opt/homebrew/lib/libfuse.2.dylib
python3 k8sfs.py ~/mnt/k8s
```

On Intel Homebrew installations, the path may be:

```bash
export FUSE_LIBRARY_PATH=/usr/local/lib/libfuse.2.dylib
```

### `permission denied` or Kubernetes API errors

Check that your kubeconfig points at the expected cluster:

```bash
kubectl config current-context
kubectl auth can-i list namespaces
kubectl auth can-i list nodes
kubectl auth can-i list pods --all-namespaces
kubectl auth can-i delete pods -n default
```

### `fusermount: command not found`

Install the FUSE package for your distribution, or use the platform-specific unmount command:

```bash
sudo apt-get install fuse libfuse2
```

or:

```bash
sudo apt-get install fuse3
fusermount3 -u ~/mnt/k8s
```

On macOS, use `umount` or `diskutil unmount` instead of `fusermount`.

### Stale entries

The script caches namespaces, nodes, Deployments, Pods, ReplicaSet ownership, and node symlink mappings for 5 seconds. Wait a few seconds and retry the command.

## Safety notes

Be careful with shell commands that recursively delete files under the mountpoint. A command such as this can delete real Kubernetes Pods:

```bash
rm ~/mnt/k8s/default/nginx/nginx-7f456874f4-bx29p
```

Avoid running broad cleanup commands such as `rm -rf` inside the mounted filesystem.
