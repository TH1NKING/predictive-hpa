package metricsprovider

import (
	"context"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type containerKey struct{ namespace, pod, container string }
type expectedContainer struct {
	podUID        types.UID
	replicaSetUID types.UID
	containerID   string
	request       float64
}
type targetRoster struct {
	generation int64
	containers map[containerKey]expectedContainer
}

func (p *PrometheusProvider) readRoster(ctx context.Context, target *appsv1.Deployment) (targetRoster, error) {
	var live appsv1.Deployment
	if err := p.reader.Get(ctx, client.ObjectKeyFromObject(target), &live); err != nil {
		return targetRoster{}, fmt.Errorf("%w: get Deployment: %v", ErrTargetChanged, err)
	}
	if live.UID != target.UID || live.DeletionTimestamp != nil {
		return targetRoster{}, ErrTargetChanged
	}
	if live.Spec.Selector == nil {
		return targetRoster{}, fmt.Errorf("%w: missing Deployment selector", ErrInvalidData)
	}
	selector, err := metav1.LabelSelectorAsSelector(live.Spec.Selector)
	if err != nil || selector.Empty() {
		return targetRoster{}, fmt.Errorf("%w: invalid Deployment selector", ErrInvalidData)
	}
	var pods corev1.PodList
	if err := p.reader.List(ctx, &pods, client.InNamespace(live.Namespace), client.MatchingLabelsSelector{Selector: selector}); err != nil {
		return targetRoster{}, fmt.Errorf("list target Pods: %w", err)
	}
	roster := targetRoster{generation: live.Generation, containers: make(map[containerKey]expectedContainer)}
	// Reuse an owner only inside this roster snapshot. The post-query snapshot
	// reads every ReplicaSet again, while API work scales with ReplicaSets, not Pods.
	replicaSets := make(map[string]appsv1.ReplicaSet)
	for _, pod := range pods.Items {
		if pod.DeletionTimestamp != nil || pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		owner := metav1.GetControllerOf(&pod)
		if owner == nil || owner.Kind != "ReplicaSet" {
			continue
		}
		rs, found := replicaSets[owner.Name]
		if !found {
			if err := p.reader.Get(ctx, types.NamespacedName{Namespace: live.Namespace, Name: owner.Name}, &rs); err != nil {
				if apierrors.IsNotFound(err) {
					return targetRoster{}, fmt.Errorf("%w: Pod owner is unavailable", ErrIncompleteData)
				}
				return targetRoster{}, fmt.Errorf("get Pod ReplicaSet: %w", err)
			}
			replicaSets[owner.Name] = rs
		}
		rsOwner := metav1.GetControllerOf(&rs)
		if rs.UID != owner.UID || rsOwner == nil || rsOwner.Kind != "Deployment" || rsOwner.UID != live.UID || rsOwner.Name != live.Name {
			continue
		}
		if err := addPodContainers(roster.containers, &pod, rs.UID); err != nil {
			return targetRoster{}, err
		}
	}
	if len(roster.containers) == 0 {
		return targetRoster{}, ErrNoData
	}
	return roster, nil
}

func addPodContainers(expected map[containerKey]expectedContainer, pod *corev1.Pod, rsUID types.UID) error {
	if pod.Spec.Resources != nil {
		return fmt.Errorf("%w: Pod-level resources are unsupported", ErrInvalidData)
	}
	for _, init := range pod.Spec.InitContainers {
		if init.RestartPolicy != nil && *init.RestartPolicy == corev1.ContainerRestartPolicyAlways {
			return fmt.Errorf("%w: restartable init sidecars are unsupported", ErrInvalidData)
		}
	}
	ready := false
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			ready = condition.Status == corev1.ConditionTrue
		}
	}
	if pod.UID == "" || pod.Status.Phase != corev1.PodRunning || !ready {
		return fmt.Errorf("%w: Pod %s is not ready", ErrIncompleteData, pod.Name)
	}
	statuses := make(map[string]corev1.ContainerStatus, len(pod.Status.ContainerStatuses))
	for _, status := range pod.Status.ContainerStatuses {
		statuses[status.Name] = status
	}
	for _, container := range pod.Spec.Containers {
		request := container.Resources.Requests.Cpu().AsApproximateFloat64()
		if !finite(request) || request <= 0 {
			return fmt.Errorf("%w: container %s/%s requires a positive CPU request", ErrInvalidData, pod.Name, container.Name)
		}
		status, ok := statuses[container.Name]
		if !ok || !status.Ready || status.State.Running == nil {
			return fmt.Errorf("%w: container %s/%s is not running", ErrIncompleteData, pod.Name, container.Name)
		}
		_, containerID, ok := strings.Cut(status.ContainerID, "://")
		if !ok || containerID == "" || strings.ContainsAny(containerID, "/\\") {
			return fmt.Errorf("%w: container instance identity is unavailable", ErrIncompleteData)
		}
		expected[containerKey{namespace: pod.Namespace, pod: pod.Name, container: container.Name}] = expectedContainer{podUID: pod.UID, replicaSetUID: rsUID, containerID: containerID, request: request}
	}
	return nil
}
