import logging
import os
import shutil
import sys

from datetime import datetime

from kubernetes import client
from kubernetes import config
from kubernetes.config import ConfigException

from pathlib import Path
from secrets import token_hex
from .constants import NBGRADER_HOME_CONFIG_TEMPLATE
from .constants import NBGRADER_COURSE_CONFIG_TEMPLATE


logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger(__name__)


NAMESPACE = 'default'
GRADER_IMAGE_NAME = os.environ.get('GRADER_IMAGE_NAME', 'illumidesk/grader-notebook:latest')
MNT_ROOT = os.environ.get('ILLUMIDESK_MNT_ROOT', 'illumidesk-courses')


# NBGrader DATABASE settings
nbgrader_db_host = os.environ.get('POSTGRES_NBGRADER_HOST')
nbgrader_db_password = os.environ.get('POSTGRES_NBGRADER_PASSWORD')
nbgrader_db_user = os.environ.get('POSTGRES_NBGRADER_USER')


class GraderServiceLauncher:
    def __init__(self, org_name: str, course_id: str):
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
        self.org_name = org_name
        self.grader_name = f'grader-{self.course_id}'
        self.grader_token = token_hex(32)
        # Course home directory, its parent should be the grader name
        self.course_dir = Path(f'/{MNT_ROOT}/{self.org_name}/home/grader-{self.course_id}/{self.course_id}')

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
    
    def grader_service_exists(self) -> bool:
        """
        Check if there is a deployment for the grader service name
        """
        # Filter deployments by the current namespace and a specific name (metadata collection)
        service_list = self.coreV1Api.list_namespaced_service(
            namespace=NAMESPACE,
            field_selector=f'metadata.name={self.grader_name}'
        )
        if service_list and service_list.items:            
            return True
        
        return False

    def create_grader_deployment(self):
        # first create the home directories for grader/course
        try:
            self._create_grader_directories()
            self._create_nbgrader_files()
        except Exception as e:
            msg = 'An error occurred trying to create directories and files for nbgrader.'
            logger.error(f'{msg}{e}')
            raise Exception(msg)
            
        # Create grader deployement
        deployment = self._create_deployment_object()
        api_response = self.apps_v1.create_namespaced_deployment(body=deployment, namespace=NAMESPACE)
        logger.info("Deployment created. status='%s'" % str(api_response.status))
        # Create grader service
        service = self._create_service_object()
        self.coreV1Api.create_namespaced_service(namespace=NAMESPACE, body=service)

    def _create_grader_directories(self):
        """
        Creates home directories with specific permissions
        Directories to create:
        - grader_root: /<org-name>/home/grader-<course-id>
        - course_root: /<org-name>/home/grader-<course-id>/<course-id>
        """
        uid = 10001
        gid = 100
        logger.debug(
            f'Create course directory "{self.course_dir}" with special permissions {uid}:{gid}'
        )
        self.course_dir.mkdir(parents=True, exist_ok=True)
        # change the course directory owner
        shutil.chown(str(self.course_dir), user=uid, group=gid)
        # change the grader-home directory owner
        shutil.chown(str(self.course_dir.parent), user=uid, group=gid)
    
    def _create_nbgrader_files(self):
        # create the .jupyter directory (a child of grader_root)
        jupyter_dir = self.course_dir.parent.joinpath('.jupyter')
        jupyter_dir.mkdir(parents=True, exist_ok=True)
        shutil.chown(str(jupyter_dir), user=10001, group=100)
        # Write the nbgrader_config.py file at grader home directory
        grader_nbconfig_path = jupyter_dir.joinpath('nbgrader_config.py')
        logger.info(f'Writing the nbgrader_config.py file at jupyter directory (within the grader home): {grader_nbconfig_path}')
        # write the file
        grader_home_nbconfig_content = NBGRADER_HOME_CONFIG_TEMPLATE.format(
            grader_name=self.grader_name,
            course_id=self.course_id,
            db_url=f'postgresql://{nbgrader_db_user}:{nbgrader_db_password}@{nbgrader_db_host}:5432/{self.org_name}_{self.course_id}'
        )
        grader_nbconfig_path.write_text(grader_home_nbconfig_content)
        # Write the nbgrader_config.py file at grader home directory
        course_nbconfig_path = self.course_dir.joinpath('nbgrader_config.py')
        logger.info(f'Writing the nbgrader_config.py file at course home directory: {course_nbconfig_path}')
        # write the file
        course_home_nbconfig_content = NBGRADER_COURSE_CONFIG_TEMPLATE.format(
            course_id=self.course_id
        )
        course_nbconfig_path.write_text(course_home_nbconfig_content)

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
            working_dir=f'/home/{self.grader_name}',
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
            ],
            volume_mounts=[client.V1VolumeMount(
                    mount_path=f'/home/{self.grader_name}',
                    name='grader-setup-pvc',
                    sub_path=f'{MNT_ROOT}/{self.org_name}/home/grader-{self.course_id}/'
                )
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
                security_context=client.V1PodSecurityContext(run_as_user=0),
                volumes=[client.V1Volume(
                    name='grader-setup-pvc',
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name='grader-setup-pvc')
                )]
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
        # first delete the service
        if self.grader_service_exists():
            self.coreV1Api.delete_namespaced_service(name=self.grader_name, namespace=NAMESPACE)
        # then delete the deployment
        if self.grader_deployment_exists():
            self.apps_v1.delete_namespaced_deployment(name=self.grader_name, namespace=NAMESPACE)

    def update_jhub_deployment(self):
        jhub_deployments = self.apps_v1.list_namespaced_deployment(
            namespace=NAMESPACE,
            label_selector='component=hub'
        )
        if jhub_deployments.items:
            # add new label with the current datetime (only used to the replacement occurs)
            for deployment in jhub_deployments.items:
                # get the jhub deployment template
                current_metadata = deployment.spec.template.metadata
                current_labels = current_metadata.labels
                # add the label
                current_labels.update({'restarted_at': datetime.now().strftime('%m_%d_%Y_%H_%M_%S')})
                current_metadata.labels = current_labels
                # update the deployment object
                deployment.spec.template.metatada = current_metadata
                api_response = self.apps_v1.patch_namespaced_deployment(
                    name='hub',
                    namespace=NAMESPACE,
                    body=deployment
                )
                logger.info(f'Jhub patch response:{api_response}')
