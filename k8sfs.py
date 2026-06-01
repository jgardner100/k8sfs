#!/usr/bin/env python3
import errno
import os
import stat
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import yaml
from fuse import FUSE, FuseOSError, LoggingMixIn, Operations
from kubernetes import client, config
from kubernetes.client.rest import ApiException


FILE_MODE = stat.S_IFREG | 0o444
DIR_MODE = stat.S_IFDIR | 0o555
SYMLINK_MODE = stat.S_IFLNK | 0o777


class K8sFS(LoggingMixIn, Operations):
    """
    Exposes Kubernetes namespaces, deployments, pods, services, ingresses, and nodes as a
    read-only filesystem, except that deleting a pod status file deletes the pod.

    Layout:

      /
        <namespace>/
          services/
            <service>/
              status
              deployment.yaml
          ingresses/
            <ingress>/
              status
              deployment.yaml
          <deployment>/
            deployment.yaml
            <pod>
        <node>/
          <pod> -> /<namespace>/<deployment>/<pod>
    """

    def __init__(self) -> None:
        try:
            config.load_kube_config()
        except Exception:
            config.load_incluster_config()

        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.networking = client.NetworkingV1Api()
        self.api_client = client.ApiClient()

    # ---------------------------------------------------------------------
    # Kubernetes helpers
    # ---------------------------------------------------------------------

    def _serialise_yaml(self, obj) -> str:
        data = self.api_client.sanitize_for_serialization(obj)
        return yaml.safe_dump(data, sort_keys=False)

    def _age(self, created_at: Optional[datetime]) -> str:
        if not created_at:
            return "<unknown>"

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        seconds = max(0, int((datetime.now(timezone.utc) - created_at).total_seconds()))
        minutes = seconds // 60
        hours = minutes // 60
        days = hours // 24

        if days:
            return f"{days}d"
        if hours:
            return f"{hours}h"
        if minutes:
            return f"{minutes}m"
        return f"{seconds}s"

    def _namespaces(self) -> List[str]:
        return sorted(ns.metadata.name for ns in self.core.list_namespace().items)

    def _nodes(self) -> List[str]:
        return sorted(node.metadata.name for node in self.core.list_node().items)

    def _deployments(self, namespace: str) -> List[str]:
        return sorted(
            deploy.metadata.name
            for deploy in self.apps.list_namespaced_deployment(namespace).items
        )

    def _services(self, namespace: str) -> List[str]:
        return sorted(
            service.metadata.name
            for service in self.core.list_namespaced_service(namespace).items
        )

    def _ingresses(self, namespace: str) -> List[str]:
        return sorted(
            ingress.metadata.name
            for ingress in self.networking.list_namespaced_ingress(namespace).items
        )

    def _read_deployment(self, namespace: str, deployment: str):
        return self.apps.read_namespaced_deployment(deployment, namespace)

    def _read_service(self, namespace: str, service: str):
        return self.core.read_namespaced_service(service, namespace)

    def _read_ingress(self, namespace: str, ingress: str):
        return self.networking.read_namespaced_ingress(ingress, namespace)

    def _deployment_selector(self, namespace: str, deployment: str) -> str:
        deploy = self._read_deployment(namespace, deployment)
        labels = {}

        selector = deploy.spec.selector if deploy.spec else None
        if selector and selector.match_labels:
            labels.update(selector.match_labels)

        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    def _pods_for_deployment(self, namespace: str, deployment: str):
        selector = self._deployment_selector(namespace, deployment)
        if not selector:
            return []

        return sorted(
            self.core.list_namespaced_pod(namespace, label_selector=selector).items,
            key=lambda pod: pod.metadata.name,
        )

    def _pod_names_for_deployment(self, namespace: str, deployment: str) -> List[str]:
        return [pod.metadata.name for pod in self._pods_for_deployment(namespace, deployment)]

    def _read_pod(self, namespace: str, pod: str):
        return self.core.read_namespaced_pod(pod, namespace)

    def _pod_path(self, namespace: str, pod_name: str) -> Optional[str]:
        for deployment in self._deployments(namespace):
            if pod_name in self._pod_names_for_deployment(namespace, deployment):
                return f"/{namespace}/{deployment}/{pod_name}"
        return None

    def _pods_on_node(self, node_name: str) -> Dict[str, str]:
        links: Dict[str, str] = {}

        for namespace in self._namespaces():
            pods = self.core.list_namespaced_pod(
                namespace,
                field_selector=f"spec.nodeName={node_name}",
            ).items

            for pod in pods:
                pod_path = self._pod_path(namespace, pod.metadata.name)
                if not pod_path:
                    continue

                link_name = pod.metadata.name

                # Pod names are only namespace-unique. If there is a collision in
                # this node directory, keep the normal name for the first pod and
                # use a namespace-qualified link for subsequent collisions.
                if link_name in links:
                    link_name = f"{namespace}__{pod.metadata.name}"

                links[link_name] = pod_path

        return dict(sorted(links.items()))

    # ---------------------------------------------------------------------
    # Rendered file content
    # ---------------------------------------------------------------------

    def _pod_status_text(self, namespace: str, pod_name: str) -> str:
        pod = self._read_pod(namespace, pod_name)
        statuses = pod.status.container_statuses or []
        ready = sum(1 for status in statuses if status.ready)
        total = len(pod.spec.containers or [])
        restarts = sum(status.restart_count or 0 for status in statuses)

        return (
            f"Ready: {ready}/{total}\n"
            f"Status: {pod.status.phase or '<unknown>'}\n"
            f"Restarts: {restarts}\n"
            f"Age: {self._age(pod.metadata.creation_timestamp)}\n"
            f"IP: {pod.status.pod_ip or '<none>'}\n"
            f"Node: {pod.spec.node_name or '<none>'}\n"
        )

    def _service_status_text(self, namespace: str, service_name: str) -> str:
        service = self._read_service(namespace, service_name)
        spec = service.spec
        status = service.status

        ports = []
        for port in spec.ports or []:
            port_bits = [str(port.port)]
            if port.target_port:
                port_bits.append(f"target={port.target_port}")
            if port.node_port:
                port_bits.append(f"node={port.node_port}")
            protocol = port.protocol or "TCP"
            ports.append(f"{'/'.join(port_bits)}/{protocol}")

        selector = "<none>"
        if spec.selector:
            selector = ",".join(f"{key}={value}" for key, value in sorted(spec.selector.items()))

        external_ips = list(spec.external_i_ps or [])
        lb = status.load_balancer if status else None
        if lb and lb.ingress:
            for ingress in lb.ingress:
                if ingress.ip:
                    external_ips.append(ingress.ip)
                elif ingress.hostname:
                    external_ips.append(ingress.hostname)

        return (
            f"Name: {service.metadata.name}\n"
            f"Type: {spec.type or '<unknown>'}\n"
            f"Cluster IP: {spec.cluster_ip or '<none>'}\n"
            f"External IP: {', '.join(external_ips) if external_ips else '<none>'}\n"
            f"Ports: {', '.join(ports) if ports else '<none>'}\n"
            f"Selector: {selector}\n"
            f"Age: {self._age(service.metadata.creation_timestamp)}\n"
        )

    def _ingress_status_text(self, namespace: str, ingress_name: str) -> str:
        ingress = self._read_ingress(namespace, ingress_name)
        spec = ingress.spec
        status = ingress.status

        # Extract hosts from rules
        hosts = []
        if spec.rules:
            for rule in spec.rules:
                if rule.host:
                    hosts.append(rule.host)

        # Extract IPs and hostnames from status
        addresses = []
        lb = status.load_balancer if status else None
        if lb and lb.ingress:
            for ingress_status in lb.ingress:
                if ingress_status.ip:
                    addresses.append(ingress_status.ip)
                elif ingress_status.hostname:
                    addresses.append(ingress_status.hostname)

        # Extract TLS hosts
        tls_hosts = []
        if spec.tls:
            for tls in spec.tls:
                if tls.hosts:
                    tls_hosts.extend(tls.hosts)

        # Extract backend services
        backend_services = []
        if spec.rules:
            for rule in spec.rules:
                if rule.http and rule.http.paths:
                    for path in rule.http.paths:
                        if path.backend and path.backend.service:
                            service = path.backend.service
                            backend_services.append(f"{service.name}:{service.port.number if service.port else 'N/A'}")

        return (
            f"Name: {ingress.metadata.name}\n"
            f"Hosts: {', '.join(hosts) if hosts else '<none>'}\n"
            f"Address: {', '.join(addresses) if addresses else '<pending>'}\n"
            f"TLS: {', '.join(tls_hosts) if tls_hosts else '<none>'}\n"
            f"Backend Services: {', '.join(backend_services) if backend_services else '<none>'}\n"
            f"Age: {self._age(ingress.metadata.creation_timestamp)}\n"
        )

    def _file_bytes(self, path: str) -> bytes:
        parts = self._parts(path)

        # /<namespace>/<deployment>/deployment.yaml
        if len(parts) == 3 and parts[0] in self._namespaces():
            namespace, deployment, filename = parts
            if filename == "deployment.yaml" and deployment in self._deployments(namespace):
                return self._serialise_yaml(self._read_deployment(namespace, deployment)).encode()

            if deployment in self._deployments(namespace):
                return self._pod_status_text(namespace, filename).encode()

        # /<namespace>/services/<service>/status
        # /<namespace>/services/<service>/deployment.yaml
        if (
            len(parts) == 4
            and parts[0] in self._namespaces()
            and parts[1] == "services"
            and parts[2] in self._services(parts[0])
        ):
            namespace, _, service_name, filename = parts

            if filename == "status":
                return self._service_status_text(namespace, service_name).encode()

            if filename == "deployment.yaml":
                # Filename requested by the user; content is the Kubernetes Service
                # object definition.
                return self._serialise_yaml(self._read_service(namespace, service_name)).encode()

        # /<namespace>/ingresses/<ingress>/status
        # /<namespace>/ingresses/<ingress>/deployment.yaml
        if (
            len(parts) == 4
            and parts[0] in self._namespaces()
            and parts[1] == "ingresses"
            and parts[2] in self._ingresses(parts[0])
        ):
            namespace, _, ingress_name, filename = parts

            if filename == "status":
                return self._ingress_status_text(namespace, ingress_name).encode()

            if filename == "deployment.yaml":
                # Filename requested by the user; content is the Kubernetes Ingress
                # object definition.
                return self._serialise_yaml(self._read_ingress(namespace, ingress_name)).encode()

        raise FuseOSError(errno.ENOENT)

    # ---------------------------------------------------------------------
    # Path helpers
    # ---------------------------------------------------------------------

    def _parts(self, path: str) -> List[str]:
        return [part for part in path.split("/") if part]

    def _is_namespace_dir(self, parts: List[str]) -> bool:
        return len(parts) == 1 and parts[0] in self._namespaces()

    def _is_node_dir(self, parts: List[str]) -> bool:
        return len(parts) == 1 and parts[0] in self._nodes()

    def _is_deployment_dir(self, parts: List[str]) -> bool:
        return (
            len(parts) == 2
            and parts[0] in self._namespaces()
            and parts[1] in self._deployments(parts[0])
        )

    def _is_services_dir(self, parts: List[str]) -> bool:
        return len(parts) == 2 and parts[0] in self._namespaces() and parts[1] == "services"

    def _is_service_dir(self, parts: List[str]) -> bool:
        return (
            len(parts) == 3
            and parts[0] in self._namespaces()
            and parts[1] == "services"
            and parts[2] in self._services(parts[0])
        )

    def _is_ingresses_dir(self, parts: List[str]) -> bool:
        return len(parts) == 2 and parts[0] in self._namespaces() and parts[1] == "ingresses"

    def _is_ingress_dir(self, parts: List[str]) -> bool:
        return (
            len(parts) == 3
            and parts[0] in self._namespaces()
            and parts[1] == "ingresses"
            and parts[2] in self._ingresses(parts[0])
        )

    def _is_deployment_file(self, parts: List[str]) -> bool:
        return (
            len(parts) == 3
            and parts[0] in self._namespaces()
            and parts[1] in self._deployments(parts[0])
            and parts[2] == "deployment.yaml"
        )

    def _is_pod_file(self, parts: List[str]) -> bool:
        return (
            len(parts) == 3
            and parts[0] in self._namespaces()
            and parts[1] in self._deployments(parts[0])
            and parts[2] in self._pod_names_for_deployment(parts[0], parts[1])
        )

    def _is_service_file(self, parts: List[str]) -> bool:
        return (
            len(parts) == 4
            and parts[0] in self._namespaces()
            and parts[1] == "services"
            and parts[2] in self._services(parts[0])
            and parts[3] in {"status", "deployment.yaml"}
        )

    def _is_ingress_file(self, parts: List[str]) -> bool:
        return (
            len(parts) == 4
            and parts[0] in self._namespaces()
            and parts[1] == "ingresses"
            and parts[2] in self._ingresses(parts[0])
            and parts[3] in {"status", "deployment.yaml"}
        )

    def _is_node_pod_symlink(self, parts: List[str]) -> bool:
        return len(parts) == 2 and parts[0] in self._nodes() and parts[1] in self._pods_on_node(parts[0])

    # ---------------------------------------------------------------------
    # FUSE operations
    # ---------------------------------------------------------------------

    def getattr(self, path: str, fh=None):
        now = time.time()
        parts = self._parts(path)

        try:
            if path == "/" or self._is_namespace_dir(parts) or self._is_node_dir(parts):
                return {
                    "st_mode": DIR_MODE,
                    "st_nlink": 2,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }

            if (
                self._is_deployment_dir(parts)
                or self._is_services_dir(parts)
                or self._is_service_dir(parts)
                or self._is_ingresses_dir(parts)
                or self._is_ingress_dir(parts)
            ):
                return {
                    "st_mode": DIR_MODE,
                    "st_nlink": 2,
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }

            if self._is_node_pod_symlink(parts):
                return {
                    "st_mode": SYMLINK_MODE,
                    "st_nlink": 1,
                    "st_size": len(self._pods_on_node(parts[0])[parts[1]]),
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }

            if (
                self._is_deployment_file(parts)
                or self._is_pod_file(parts)
                or self._is_service_file(parts)
                or self._is_ingress_file(parts)
            ):
                return {
                    "st_mode": FILE_MODE,
                    "st_nlink": 1,
                    "st_size": len(self._file_bytes(path)),
                    "st_ctime": now,
                    "st_mtime": now,
                    "st_atime": now,
                }

        except ApiException as exc:
            if exc.status == 404:
                raise FuseOSError(errno.ENOENT)
            raise

        raise FuseOSError(errno.ENOENT)

    def readdir(self, path: str, fh):
        parts = self._parts(path)

        try:
            if path == "/":
                return [".", "..", *self._namespaces(), *self._nodes()]

            if self._is_namespace_dir(parts):
                namespace = parts[0]
                return [".", "..", "services", "ingresses", *self._deployments(namespace)]

            if self._is_deployment_dir(parts):
                namespace, deployment = parts
                return [
                    ".",
                    "..",
                    "deployment.yaml",
                    *self._pod_names_for_deployment(namespace, deployment),
                ]

            if self._is_services_dir(parts):
                namespace = parts[0]
                return [".", "..", *self._services(namespace)]

            if self._is_service_dir(parts):
                return [".", "..", "status", "deployment.yaml"]

            if self._is_ingresses_dir(parts):
                namespace = parts[0]
                return [".", "..", *self._ingresses(namespace)]

            if self._is_ingress_dir(parts):
                return [".", "..", "status", "deployment.yaml"]

            if self._is_node_dir(parts):
                return [".", "..", *self._pods_on_node(parts[0]).keys()]

        except ApiException as exc:
            if exc.status == 404:
                raise FuseOSError(errno.ENOENT)
            raise

        raise FuseOSError(errno.ENOENT)

    def read(self, path: str, size: int, offset: int, fh):
        data = self._file_bytes(path)
        return data[offset : offset + size]

    def readlink(self, path: str):
        parts = self._parts(path)
        if self._is_node_pod_symlink(parts):
            return self._pods_on_node(parts[0])[parts[1]]
        raise FuseOSError(errno.ENOENT)

    def unlink(self, path: str):
        parts = self._parts(path)

        if not self._is_pod_file(parts):
            raise FuseOSError(errno.EPERM)

        namespace, _, pod_name = parts
        self.core.delete_namespaced_pod(pod_name, namespace)
        return 0


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Expose a Kubernetes cluster as a FUSE filesystem")
    parser.add_argument("mountpoint")
    parser.add_argument("-f", "--foreground", action="store_true", help="run in the foreground")
    args = parser.parse_args()

    FUSE(
        K8sFS(),
        args.mountpoint,
        foreground=args.foreground,
        nothreads=True,
        allow_other=False,
    )


if __name__ == "__main__":
    main()
