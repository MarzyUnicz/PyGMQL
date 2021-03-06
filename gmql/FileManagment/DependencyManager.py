import xml.etree.ElementTree as ET
import requests
from requests.exceptions import ConnectionError
from xml.dom import minidom
from . import get_resources_dir, get_user_dir
import os
from glob import glob
from tqdm import tqdm
import logging

CHUNK_SIZE = 1 * 1024 * 1024  # 5 MB
backend_name = "GMQL-PythonAPI"


class DependencyManager:
    def __init__(self):

        self.logger = logging.getLogger()

        self.dependency_file_path = os.path.join(get_resources_dir(), "dependencies.xml")
        self.repo_name, self.repo_url, self.deps = self._parse_dependency_file(self.dependency_file_path)
        self.backend_info_file = os.path.join(get_user_dir(), "dependencies_info.xml")
        backend_info = None
        if os.path.isfile(self.backend_info_file):
            backend_info = self._parse_dependency_info(self.backend_info_file)
        self.backend_info = backend_info
        backend_jar_path = glob(os.path.join(get_user_dir(), "{}*.jar".format(backend_name)))
        backend_jar_path = backend_jar_path[0] if len(backend_jar_path) == 1 else None
        self.backend_jar_path = backend_jar_path

    def is_backend_present(self):
        return self.backend_info is not None

    def is_connection_on(self):
        pass

    @staticmethod
    def _parse_dependency_file(path):
        tree = ET.parse(path)
        root = tree.getroot()

        repository = root.find("repository")
        repo_name = repository.find("name").text
        repo_url = repository.find("url").text

        if repo_url[-1] == '/':
            repo_url = repo_url[:-1]

        deps = []
        dependencies = root.find("dependencies")
        if dependencies is not None:
            for d in dependencies:
                groupId = d.find("groupId").text  # mandatory
                artifactId = d.find("artifactId").text  # mandatory
                version = d.find("version").text  # mandatory
                dd = {
                    'groupId': groupId,
                    "artifactId": artifactId,
                    "version": version
                }
                classifier = d.find("classifier")  # optional
                if classifier is not None:
                    classifier = classifier.text
                    dd['classifier'] = classifier
                deps.append(dd)
        return repo_name, repo_url, deps

    @staticmethod
    def _parse_dependency_info(path):
        tree = ET.parse(path)
        return DependencyManager.__parse_dependency_info_from_tree(tree)

    @staticmethod
    def _parse_dependency_info_fromstring(s):
        tree = ET.ElementTree(ET.fromstring(s))
        return DependencyManager.__parse_dependency_info_from_tree(tree)

    @staticmethod
    def __parse_dependency_info_from_tree(tree):
        root = tree.getroot()
        data = root.find("data")
        res = {}
        for d in data:
            tag = d.tag
            text = d.text
            try:
                n = int(text)
                text = n
            except ValueError:
                pass
            res[tag] = text
        return res

    @staticmethod
    def find_package(repo, repo_name, groupId, artifactId, version, classifier=None):
        first_part_url = "/".join(repo.split("/")[:3]) + "/"
        query_url = first_part_url + "service/local/artifact/maven/resolve?"
        query_url += "g={}&".format(groupId)
        query_url += "a={}&".format(artifactId)
        query_url += "v={}&".format(version)
        query_url += "r={}".format(repo_name)
        if classifier is not None:
            query_url += "&c={}".format(classifier)
        resp_text = requests.get(query_url).text
        resp = DependencyManager._parse_dependency_info_fromstring(resp_text)
        location = repo + resp['repositoryPath']
        return location

    def resolve_dependencies(self):
        first_part_url = "/".join(self.repo_url.split("/")[:3]) + "/"
        query_url = first_part_url + "/service/local/artifact/maven/resolve?"
        for d in self.deps:
            query_url += "g={}&".format(d['groupId'])
            query_url += "a={}&".format(d['artifactId'])
            query_url += "v={}&".format(d['version'])
            query_url += "r={}".format(self.repo_name)
            if "classifier" in d.keys():
                query_url += "&c={}".format(d['classifier'])
            try:
                resp_text = requests.get(query_url).text
            except ConnectionError:
                if self.is_backend_present():
                    return self.backend_jar_path
                else:
                    raise ValueError("Unable to connect to repository to retrieve GMQL backend. Check your connection")
            resp = self._parse_dependency_info_fromstring(resp_text)
            location = self.repo_url + resp['repositoryPath']
            output_path = os.path.join(get_user_dir(), resp['repositoryPath'].split("/")[-1])
            if not self.is_backend_present():
                self.logger.info("Downloading backend")
                # there is no backend (first start)
                self.download_from_location(location, output_path)
                self._save_dependency(resp_text)
            elif self.repo_name == 'snapshots' and \
                    self.backend_info['snapshot'] == 'true':
                # we have a snapshot backend and we are pulling a snapshot
                current_timestamp = self.backend_info['snapshotTimeStamp']
                retrieved_timestamp = resp['snapshotTimeStamp']
                if current_timestamp < retrieved_timestamp:
                    # we are using an outdated backend
                    self.logger.info("Updating backend to latest version")
                    self.__set_backend(location, output_path, resp_text)
            elif self.repo_name == 'releases':
                # If we need a release, it always wins
                if self.backend_info['snapshot'] == 'true':
                    self.logger.info("Updating backend")
                    self.__set_backend(location, output_path, resp_text)
                else:
                    current_version = float(self.backend_info['version'])
                    retrieved_version = float(resp['version'])
                    current_classifier = self.backend_info.get("classifier")
                    retrieved_classifier = resp.get("classifier")
                    if (current_version != retrieved_version) or (current_classifier != retrieved_classifier):
                        # the versions do not match
                        self.logger.info("Updating backend")
                        self.__set_backend(location, output_path, resp_text)
            else:
                raise NotImplementedError("Need to implement the backend download in the case"
                                          " of releases!!!!")
            return output_path

    def __set_backend(self, location, output_path, resp_text):
        self._delete_current_backend()
        self.download_from_location(location, output_path)
        self._save_dependency(resp_text)

    @staticmethod
    def _save_dependency(resp):
        resp_nice = minidom.parseString(resp)
        resp_nice = resp_nice.toprettyxml()
        with open(os.path.join(get_user_dir(), "dependencies_info.xml"), "w") as f:
            f.write(resp_nice)

    @staticmethod
    def download_from_location(location, output_path):
        r = requests.get(location, stream=True)
        total_size = int(r.headers.get("content-length", 0))
        with open(output_path, "wb") as f:
            for data in tqdm(r.iter_content(chunk_size=CHUNK_SIZE), total=int(total_size/CHUNK_SIZE), unit="M"):
                f.write(data)

    def _delete_current_backend(self):
        # search for the only jar file in the resources path
        os.remove(self.backend_jar_path)
        os.remove(self.backend_info_file)
