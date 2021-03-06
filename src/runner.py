from mct_config import Configuration
from script_exec import execute_script
from environment import config_environment
from terraform_provider import TerraformProvider
import argparse
import os
import base64
from pathlib import Path
import requests
import json
import subprocess

from logger import Logger, log, setup_logs

def get_parameters():
    parser = argparse.ArgumentParser(description="MCTv2 Alpha")

    parser.add_argument('--config_path')
    parser.add_argument('--secrets_path')
    parser.add_argument('--terraform_workspace')
    parser.add_argument('--log_path')
    parser.add_argument('--tests_path')
    parser.add_argument('--steps')
    parser.add_argument('--id')
    parser.add_argument('--custom_fields')
    parser.add_argument('--debug', dest='debug', action='store_true')

    return parser.parse_args()


def check_parameters(parameters):
    if not parameters.id:
        raise Exception("id parameter is empty")

    if not parameters.config_path:
        raise Exception("config_path parameter is empty")
    parameters.config_path = os.path.abspath(parameters.config_path)

    if parameters.secrets_path:
        parameters.secrets_path = os.path.abspath(parameters.secrets_path)

    if not parameters.terraform_workspace:
        raise Exception("terraform_workspace parameter is empty")
    parameters.terraform_workspace = os.path.abspath(parameters.terraform_workspace)

    if not os.path.isfile(parameters.config_path):
        raise Exception("File {} does not exist".format(parameters.config_path))
    if not os.path.isdir(parameters.terraform_workspace):
        raise Exception("File {} does not exist".format(parameters.terraform_workspace))

    return parameters


def config_azure_env(secrets):
    os.environ["ARM_CLIENT_ID"] = secrets["azure"]["client-id"]
    os.environ["ARM_CLIENT_SECRET"] = secrets["azure"]["client-secret"]
    os.environ["ARM_SUBSCRIPTION_ID"] = secrets["azure"]["subscription-id"]
    os.environ["ARM_TENANT_ID"] = secrets["azure"]["tenant-id"]


def config_devops_env(secrets):
    os.environ["AZDO_PERSONAL_ACCESS_TOKEN"] = secrets["azuredevops"]["token"]
    os.environ["AZDO_ORG_SERVICE_URL"] = secrets["azuredevops"]["service-url"]


def get_azure_backend_config(key, secrets):
    return {"storage_account_name": secrets["storage-account-name"],
            "container_name": secrets["storage-account-container"],
            "key": key,
            "sas_token": secrets["storage-account-sas"]}


def configure_terraform_cloud_secrets(secrets):
    secrets_entry = 'credentials "app.terraform.io" {0}'.format('{ token = "' + secrets["token"] + '" }')

    Path("~/.terraform.d").mkdir(parents=True, exist_ok=True)
    with open("~/.terraform.d/credentials.tfrc.json", "a") as terraform_config:
        terraform_config.write(secrets_entry)


def deploy_infrastructure(id, secrets, steps, config, workspace):
    config["parameters"]["id"] = id
    tf_controller = Terraform(working_dir=workspace,
                              variables=config["parameters"])

    

    backend_config = None
    if "backend" in config.keys():
        if config["backend"]["type"] == "azurerm":
            backend_config = get_azure_backend_config(config["backend"]["key"], secrets["azurerm-backend"])
    tf_controller.init(capture_output=False, backend_config=backend_config)

    if config["backend"]["type"] == "azurerm":
        workspace_list = tf_controller.cmd('workspace', 'list')[1]
        workspace_list = workspace_list.replace('\n', '').replace('*', '').split()

        if id not in workspace_list:
            tf_controller.create_workspace(id)
        tf_controller.set_workspace(id)
    elif config["backend"]["type"] == "terraform-cloud":
        url = "https://app.terraform.io/api/v2/organizations/{0}/workspaces/{1}".format(config["backend"]["org"], config["backend"]["workspace"])
        header = {"Authorization": "Bearer {}".format(secrets["terraform-cloud-backend"]["token"]),
                  "Content-Type": "application/vnd.api+json"}
        data = {"data": {"type": "workspaces", "attributes": {"operations": False}}}

        requests.patch(url, data=json.dumps(data), headers=header)

    if "deploy" in steps:
        tf_controller.plan(capture_output=False)
        tf_controller.apply(capture_output=False, skip_plan=True)
        return tf_controller.output()

    if "destroy" in steps:
        tf_controller.destroy(capture_output=False)

        if config["backend"]["type"] == "azurerm":
            tf_controller.delete_workspace(id)
        elif config["backend"]["type"] == "terraform-cloud":
            url = "https://app.terraform.io/api/v2/organizations/{0}/workspaces/{1}".format(config["backend"]["org"], config["backend"]["workspace"])
            header = {"Authorization": "Bearer {}".format(secrets["terraform-cloud-backend"]["token"]),
                      "Content-Type": "application/vnd.api+json"}

            requests.delete(url, headers=header)
        return None


def test_infrastructure(tests_path, deployment_output):
    with open("/tmp/inspec_gcp_secret.json", "w") as secret_file:
        secret_file.write(deployment_output['gcp_project_service_account']['value'])
        secret_file.close()

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/inspec_gcp_secret.json"
    os.environ["CHEF_LICENSE"] = "accept"

    test_dirs = [ f.path for f in os.scandir(tests_path) if f.is_dir() ]    
    for test_dir in test_dirs:
        test_name = os.path.basename(os.path.normpath(test_dir))
        print("Running test:", test_name)
        try:
            output = subprocess.check_output(["inspec", "exec", test_dir, "-t", "gcp://", "--input", "gcp_project_id={0}".format(deployment_output['gcp_project_id']['value'])], universal_newlines=True)
            print(output)
            print("Test result: PASS")
        except subprocess.CalledProcessError as err:
            output = err.output
            print(err.output)
            print("Test result: FAIL", "({0})".format(err.returncode))
        finally:
            with open(os.path.join(test_dir, "{0}_result.log".format(test_name)), 'w') as log_file:
                log_file.write(output)


if __name__ == "__main__":
    os.system('clear')
    print("MCTv2")
    parameters = get_parameters()
    parameters = check_parameters(parameters)
    setup_logs(parameters.log_path, parameters.id, parameters.debug)

    config = Configuration()
    config.parse_config(parameters.config_path, parameters.secrets_path)
    config.resolve_custom_fields(parameters)
    config.replace_parameters(parameters)
    config.resolve_secrets()

    config_environment(config.content, parameters.terraform_workspace)

    provider = TerraformProvider(config.content, parameters.terraform_workspace)

    for step in config.content["steps"]:
        if step == "deploy":
            provider.deploy()
        if step == "destroy":
            provider.destroy()



    # if "prepare" in config.content["steps"] and "prepare" in config.content["script"].keys():
    #     execute_script(config.content["script"]["prepare"], config.content["script"]["env"])

    #output = deploy_infrastructure(parameters.id,
    #                      config.content["secrets"],
    #                      config.content["steps"],
    #                      config.content["terraform"],
    #                      parameters.terraform_workspace)

    # if 'deploy' in config.content["steps"] and parameters.tests_path:
    #     test_infrastructure(parameters.tests_path, output)

    # if "cleanup" in config.content["steps"] and "cleanup" in config.content["script"].keys():
    #     execute_script(config.content["script"]["cleanup"], config.content["script"]["env"])
