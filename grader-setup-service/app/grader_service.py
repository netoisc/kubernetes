import os
from secrets import token_hex

from kubernetes import client
from kubernetes import config
from kubernetes.config import ConfigException


NAMESPACE = 'default'
GRADER_IMAGE_NAME = os.environ.get('GRADER_IMAGE_NAME', 'illumidesk/grader-notebook:latest')


class GraderServiceLauncher:
    def __init__(self, course_id: str):
        # try to load the cluster credentials
        try:
            # Configs can be set in Configuration class directly or using helper utility
            config.load_incluster_config()
        except ConfigException:
            # next method uses the KUBECONFIG env var by default
            config.load_kube_config()
        # Uncomment the following lines to enable debug logging
        # c = client.Configuration()
        # c.debug = True
        # apps_v1 = client.AppsV1Api(api_client=client.ApiClient(configuration=c))
        self.apps_v1 = client.AppsV1Api()
        self.coreV1Api = client.CoreV1Api()
        self.course_id = course_id
        self.grader_name = f'grader-{self.course_id}'
        self.grader_token = token_hex(32)

    def grader_deployment_exists(self) -> bool:
        """
        Check if there is a deployment for the grader service name
        """
        # Filter deployments by the current namespace and a specific name (metadata collection)
        deployment_list = self.apps_v1.list_namespaced_deployment(
            namespace=NAMESPACE,
            field_selector=f'metadata.name={self.grader_name}'
        )
        if deployment_list and deployment_list.items:            
            return True
        
        return False

    def create_grader_deployment(self):
        # Create grader deployement
        deployment = self._create_deployment_object()
        api_response = self.apps_v1.create_namespaced_deployment(body=deployment, namespace=NAMESPACE)
        print("Deployment created. status='%s'" % str(api_response.status))
        # Create grader service
        service = self._create_service_object()
        self.coreV1Api.create_namespaced_service(namespace=NAMESPACE, body=service)
    
    def _create_service_object(self):
        service = client.V1Service(
            kind='Service',
            metadata=client.V1ObjectMeta(name=self.grader_name),
            spec=client.V1ServiceSpec(
                type='ClusterIP',
                ports=[client.V1ServicePort(port=8888, target_port=8888, protocol='TCP')],
                selector={'component': self.grader_name}
            )
        )
        return service

    def _create_deployment_object(self):        
        # Configureate Pod template container
        container = client.V1Container(
            name='grader-notebook',
            image=GRADER_IMAGE_NAME,
            command=['start-notebook.sh', f'--group=formgrade-{self.course_id}'],
            ports=[client.V1ContainerPort(container_port=8888)],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "200Mi"}, limits={"cpu": "500m", "memory": "500Mi"}
            ),
            security_context=client.V1SecurityContext(allow_privilege_escalation=False),
            env=[
                client.V1EnvVar(name='JUPYTERHUB_SERVICE_NAME', value=self.course_id),
                client.V1EnvVar(name='JUPYTERHUB_API_TOKEN', value=self.grader_token),
                # we're using the K8s Service name 'hub' (defined in the jhub helm chart) 
                # to connect from our grader-notebooks
                client.V1EnvVar(name='JUPYTERHUB_API_URL', value='http://hub:8081/hub/api'),
                client.V1EnvVar(name='JUPYTERHUB_BASE_URL', value='/'),
                client.V1EnvVar(name='JUPYTERHUB_SERVICE_PREFIX', value=f'/services/{self.course_id}'),
                client.V1EnvVar(name='JUPYTERHUB_CLIENT_ID', value=f'service-{self.course_id}'),
                client.V1EnvVar(name='JUPYTERHUB_USER', value=self.grader_name),
                client.V1EnvVar(name='NB_GRADER_UID', value='10001'),
                client.V1EnvVar(name='NB_GID', value='100'),
                client.V1EnvVar(name='NB_USER', value=self.grader_name),
                # todo: validate if this env var is still required
                client.V1EnvVar(name='USER_ROLE', value='Grader'),
            ]
        )
        # Create and configurate a spec section
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={
                    'component': self.grader_name,
                    'app': 'illumidesk'}
            ),
            spec=client.V1PodSpec(
                containers=[container],
                security_context=client.V1PodSecurityContext(run_as_user=0)
            )
        )
        # Create the specification of deployment
        spec = client.V1DeploymentSpec(
            replicas=1, template=template, selector={'matchLabels': {'component': self.grader_name}}
        )
        # Instantiate the deployment object
        deployment = client.V1Deployment(
            api_version="apps/v1", kind="Deployment", metadata=client.V1ObjectMeta(name=self.grader_name), spec=spec
        )

        return deployment

    def delete_grader_deployment(self):
        try:
            self.coreV1Api.delete_namespaced_service(name=self.grader_name, namespace=NAMESPACE)
        except:
            # maybe the objects not exist
            pass
        try:
            self.apps_v1.delete_namespaced_deployment(name=self.grader_name, namespace=NAMESPACE)
        except:
            # maybe the objects not exist
            pass
