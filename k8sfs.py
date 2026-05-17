#!/usr/bin/env python3

import os

# Needed on macOS when fusepy cannot auto-detect macFUSE.
for lib in (
    "/usr/local/lib/libfuse.2.dylib",
    "/usr/local/lib/libfuse.dylib",
    "/opt/homebrew/lib/libfuse.2.dylib",
    "/opt/homebrew/lib/libfuse.dylib",
):
    if os.path.exists(lib):
        os.environ.setdefault("FUSE_LIBRARY_PATH", lib)
        break

import errno
import stat
import time
from datetime import datetime, timezone

from fuse import FUSE, Operations, FuseOSError
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException


class K8sNamespaceFS(Operations):
    def __init__(self):
        try:
            config.load_kube_config()
        except ConfigException:
            config.load_incluster_config()

        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

        self.cache_ttl_seconds = 5

        self._namespace_cache = []
        self._namespace_cache_time = 0

        self._deployment_cache = {}
        self._deployment_cache_time = {}

        # Key: (namespace, deployment)
        # Value: {pod_name: pod_object}
        self._pod_cache = {}
        self._pod_cache_time = {}

        self._node_cache = []
        self._node_cache_time = 0

        # Key: node_name
        # Value: {link_name: target_path}
        self._node_pod_links_cache = {}
        self._node_pod_links_cache_time = {}

        # Key: (namespace, replicaset_name)
        # Value: deployment_name or None
        self._replicaset_deployment_cache = {}
        self._replicaset_deployment_cache_time = {}

    def unlink(self, path):
        parts = path.strip("/").split("/")

        # Node pod entries are virtual symlinks. Do not allow deleting the
        # symlink path, because it does not represent the real pod file.
        if len(parts) == 2 and parts[0] in self._nodes():
            raise FuseOSError(errno.EPERM)

        # Only real pod files can be deleted:
        # /namespace/deployment/pod
        if len(parts) != 3:
            raise FuseOSError(errno.EISDIR)

        namespace, deployment, pod_name = parts

        if namespace not in self._namespaces():
            raise FuseOSError(errno.ENOENT)

        if deployment not in self._deployments(namespace):
            raise FuseOSError(errno.ENOENT)

        pod = self._pod_for_deployment(namespace, deployment, pod_name)

        if not pod:
            raise FuseOSError(errno.ENOENT)

        try:
            self.core_v1.delete_namespaced_pod(
                name=pod_name,
                namespace=namespace,
                body=client.V1DeleteOptions(),
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                raise FuseOSError(errno.ENOENT)

            if e.status == 403:
                raise FuseOSError(errno.EACCES)

            raise FuseOSError(errno.EIO)

        # Invalidate pod cache so `ls` updates quickly.
        cache_key = (namespace, deployment)
        self._pod_cache.pop(cache_key, None)
        self._pod_cache_time.pop(cache_key, None)

        # Invalidate the node symlink cache for the node this pod was on.
        node_name = pod.spec.node_name
        if node_name:
            self._node_pod_links_cache.pop(node_name, None)
            self._node_pod_links_cache_time.pop(node_name, None)

        return 0

    def _dir_attrs(self):
        now = int(time.time())

        return {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_nlink": 2,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
        }

    def _file_attrs(self, content):
        now = int(time.time())
        data = content.encode("utf-8")

        return {
            "st_mode": stat.S_IFREG | 0o444,
            "st_nlink": 1,
            "st_size": len(data),
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
        }

    def _symlink_attrs(self, target):
        now = int(time.time())
        data = target.encode("utf-8")

        return {
            "st_mode": stat.S_IFLNK | 0o777,
            "st_nlink": 1,
            "st_size": len(data),
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
        }

    def _namespaces(self):
        now = time.time()

        if now - self._namespace_cache_time > self.cache_ttl_seconds:
            result = self.core_v1.list_namespace()
            self._namespace_cache = sorted(
                ns.metadata.name for ns in result.items
            )
            self._namespace_cache_time = now

        return self._namespace_cache

    def _nodes(self):
        now = time.time()

        if now - self._node_cache_time > self.cache_ttl_seconds:
            result = self.core_v1.list_node()
            self._node_cache = sorted(
                node.metadata.name for node in result.items
            )
            self._node_cache_time = now

        return self._node_cache

    def _deployments(self, namespace):
        now = time.time()
        last_fetch = self._deployment_cache_time.get(namespace, 0)

        if now - last_fetch > self.cache_ttl_seconds:
            result = self.apps_v1.list_namespaced_deployment(namespace=namespace)
            self._deployment_cache[namespace] = sorted(
                deploy.metadata.name for deploy in result.items
            )
            self._deployment_cache_time[namespace] = now

        return self._deployment_cache.get(namespace, [])

    def _deployment(self, namespace, deployment):
        try:
            return self.apps_v1.read_namespaced_deployment(
                name=deployment,
                namespace=namespace,
            )
        except Exception:
            return None

    def _deployment_for_replicaset(self, namespace, replicaset_name):
        now = time.time()
        cache_key = (namespace, replicaset_name)
        last_fetch = self._replicaset_deployment_cache_time.get(cache_key, 0)

        if now - last_fetch > self.cache_ttl_seconds:
            deployment_name = None

            try:
                replica_set = self.apps_v1.read_namespaced_replica_set(
                    name=replicaset_name,
                    namespace=namespace,
                )

                for owner in replica_set.metadata.owner_references or []:
                    if owner.kind == "Deployment":
                        deployment_name = owner.name
                        break

            except client.exceptions.ApiException:
                deployment_name = None

            self._replicaset_deployment_cache[cache_key] = deployment_name
            self._replicaset_deployment_cache_time[cache_key] = now

        return self._replicaset_deployment_cache.get(cache_key)

    def _deployment_for_pod(self, pod):
        namespace = pod.metadata.namespace

        for owner in pod.metadata.owner_references or []:
            if owner.kind == "Deployment":
                return owner.name

            if owner.kind == "ReplicaSet":
                return self._deployment_for_replicaset(namespace, owner.name)

        return None

    def _pod_links_for_node(self, node_name):
        now = time.time()
        last_fetch = self._node_pod_links_cache_time.get(node_name, 0)

        if now - last_fetch > self.cache_ttl_seconds:
            result = self.core_v1.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}",
            )

            links = {}

            for pod in result.items:
                namespace = pod.metadata.namespace
                pod_name = pod.metadata.name
                deployment = self._deployment_for_pod(pod)

                # Only Deployment-owned pods have a valid target path of:
                # /namespace/deployment/pod
                if not deployment:
                    continue

                # Include namespace in the link name to avoid collisions between
                # same-named pods in different namespaces on the same node.
                link_name = f"{namespace}__{pod_name}"
                target = f"../{namespace}/{deployment}/{pod_name}"

                links[link_name] = target

            self._node_pod_links_cache[node_name] = links
            self._node_pod_links_cache_time[node_name] = now

        return self._node_pod_links_cache.get(node_name, {})

    def _label_selector_from_deployment(self, deployment_obj):
        selector = deployment_obj.spec.selector

        if not selector:
            return ""

        parts = []

        if selector.match_labels:
            for key, value in selector.match_labels.items():
                parts.append(f"{key}={value}")

        if selector.match_expressions:
            for expr in selector.match_expressions:
                key = expr.key
                operator = expr.operator
                values = expr.values or []

                if operator == "In":
                    parts.append(f"{key} in ({','.join(values)})")
                elif operator == "NotIn":
                    parts.append(f"{key} notin ({','.join(values)})")
                elif operator == "Exists":
                    parts.append(key)
                elif operator == "DoesNotExist":
                    parts.append(f"!{key}")

        return ",".join(parts)

    def _pod_map_for_deployment(self, namespace, deployment):
        now = time.time()
        cache_key = (namespace, deployment)
        last_fetch = self._pod_cache_time.get(cache_key, 0)

        if now - last_fetch > self.cache_ttl_seconds:
            deployment_obj = self._deployment(namespace, deployment)

            if not deployment_obj:
                self._pod_cache[cache_key] = {}
                self._pod_cache_time[cache_key] = now
                return {}

            label_selector = self._label_selector_from_deployment(deployment_obj)

            if not label_selector:
                self._pod_cache[cache_key] = {}
                self._pod_cache_time[cache_key] = now
                return {}

            result = self.core_v1.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
            )

            self._pod_cache[cache_key] = {
                pod.metadata.name: pod
                for pod in result.items
            }
            self._pod_cache_time[cache_key] = now

        return self._pod_cache.get(cache_key, {})

    def _pod_ip(self, pod):
        return pod.status.pod_ip or "None"

    def _pod_node(self, pod):
        return pod.spec.node_name or "None"

    def _pods_for_deployment(self, namespace, deployment):
        return sorted(
            self._pod_map_for_deployment(namespace, deployment).keys()
        )

    def _pod_for_deployment(self, namespace, deployment, pod_name):
        return self._pod_map_for_deployment(namespace, deployment).get(pod_name)

    def _pod_ready(self, pod):
        statuses = pod.status.container_statuses or []
        total = len(statuses)

        if total == 0 and pod.spec and pod.spec.containers:
            total = len(pod.spec.containers)

        ready = sum(1 for status in statuses if status.ready)

        return f"{ready}/{total}"

    def _pod_restarts(self, pod):
        statuses = pod.status.container_statuses or []
        return sum(status.restart_count or 0 for status in statuses)

    def _pod_status(self, pod):
        if pod.metadata.deletion_timestamp:
            return "Terminating"

        init_statuses = pod.status.init_container_statuses or []
        container_statuses = pod.status.container_statuses or []

        for status in init_statuses:
            state = status.state

            if state and state.waiting and state.waiting.reason:
                return f"Init:{state.waiting.reason}"

            if state and state.terminated and state.terminated.exit_code != 0:
                reason = state.terminated.reason or "Error"
                return f"Init:{reason}"

        for status in container_statuses:
            state = status.state

            if state and state.waiting and state.waiting.reason:
                return state.waiting.reason

            if state and state.terminated and state.terminated.reason:
                if pod.status.phase != "Succeeded":
                    return state.terminated.reason

        return pod.status.phase or "Unknown"

    def _pod_age(self, pod):
        created = pod.metadata.creation_timestamp

        if not created:
            return "unknown"

        now = datetime.now(timezone.utc)

        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        seconds = int((now - created).total_seconds())

        if seconds < 0:
            seconds = 0

        if seconds < 60:
            return f"{seconds}s"

        minutes = seconds // 60

        # Matches your requested style: 161m rather than 2h41m.
        if minutes < 1440:
            return f"{minutes}m"

        days = minutes // 1440
        return f"{days}d"

    def _pod_file_content(self, pod):
        return (
            f"Ready: {self._pod_ready(pod)}\n"
            f"Status: {self._pod_status(pod)}\n"
            f"Restarts: {self._pod_restarts(pod)}\n"
            f"Age: {self._pod_age(pod)}\n"
            f"IP: {self._pod_ip(pod)}\n"
            f"Node: {self._pod_node(pod)}\n"
        )

    def getattr(self, path, fh=None):
        # /
        if path == "/":
            return self._dir_attrs()

        parts = path.strip("/").split("/")

        # /namespace or /node
        if len(parts) == 1:
            name = parts[0]

            if name in self._namespaces():
                return self._dir_attrs()

            if name in self._nodes():
                return self._dir_attrs()

            raise FuseOSError(errno.ENOENT)

        # /namespace/deployment or /node/symlink
        if len(parts) == 2:
            first, second = parts

            if first in self._namespaces():
                namespace = first
                deployment = second

                if deployment in self._deployments(namespace):
                    return self._dir_attrs()

                raise FuseOSError(errno.ENOENT)

            if first in self._nodes():
                node_name = first
                link_name = second
                target = self._pod_links_for_node(node_name).get(link_name)

                if target:
                    return self._symlink_attrs(target)

                raise FuseOSError(errno.ENOENT)

            raise FuseOSError(errno.ENOENT)

        # /namespace/deployment/pod
        if len(parts) == 3:
            namespace, deployment, pod_name = parts

            if namespace not in self._namespaces():
                raise FuseOSError(errno.ENOENT)

            if deployment not in self._deployments(namespace):
                raise FuseOSError(errno.ENOENT)

            pod = self._pod_for_deployment(namespace, deployment, pod_name)

            if pod:
                return self._file_attrs(self._pod_file_content(pod))

            raise FuseOSError(errno.ENOENT)

        raise FuseOSError(errno.ENOENT)

    def readdir(self, path, fh):
        # ls /
        if path == "/":
            yield "."
            yield ".."

            for namespace in self._namespaces():
                yield namespace

            for node in self._nodes():
                yield node

            return

        parts = path.strip("/").split("/")

        # ls /namespace or ls /node
        if len(parts) == 1:
            name = parts[0]

            if name in self._namespaces():
                yield "."
                yield ".."

                for deployment in self._deployments(name):
                    yield deployment

                return

            if name in self._nodes():
                yield "."
                yield ".."

                for link_name in sorted(self._pod_links_for_node(name).keys()):
                    yield link_name

                return

            raise FuseOSError(errno.ENOENT)

        # ls /namespace/deployment
        if len(parts) == 2:
            namespace, deployment = parts

            if namespace not in self._namespaces():
                raise FuseOSError(errno.ENOENT)

            if deployment not in self._deployments(namespace):
                raise FuseOSError(errno.ENOENT)

            yield "."
            yield ".."

            for pod in self._pods_for_deployment(namespace, deployment):
                yield pod

            return

        raise FuseOSError(errno.ENOTDIR)

    def readlink(self, path):
        parts = path.strip("/").split("/")

        # /node/link
        if len(parts) != 2:
            raise FuseOSError(errno.EINVAL)

        node_name, link_name = parts

        if node_name not in self._nodes():
            raise FuseOSError(errno.ENOENT)

        target = self._pod_links_for_node(node_name).get(link_name)

        if not target:
            raise FuseOSError(errno.ENOENT)

        return target

    def open(self, path, flags):
        parts = path.strip("/").split("/")

        if len(parts) != 3:
            raise FuseOSError(errno.EISDIR)

        namespace, deployment, pod_name = parts
        pod = self._pod_for_deployment(namespace, deployment, pod_name)

        if not pod:
            raise FuseOSError(errno.ENOENT)

        return 0

    def read(self, path, size, offset, fh):
        parts = path.strip("/").split("/")

        if len(parts) != 3:
            raise FuseOSError(errno.EISDIR)

        namespace, deployment, pod_name = parts
        pod = self._pod_for_deployment(namespace, deployment, pod_name)

        if not pod:
            raise FuseOSError(errno.ENOENT)

        data = self._pod_file_content(pod).encode("utf-8")

        return data[offset:offset + size]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <mountpoint>")
        sys.exit(1)

    mountpoint = sys.argv[1]

    FUSE(
        K8sNamespaceFS(),
        mountpoint,
        foreground=True,
        nothreads=True,
    )
