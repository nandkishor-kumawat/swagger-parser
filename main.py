import requests
import json
import os
import re
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()


def pf(data):
    print('\n', json.dumps(data, indent=3), '\n')


class SwaggerParser:

    """
    SwaggerParser class is used to parse the swagger json data and generate test controller for each tag.

    Example usage:
    parser = SwaggerParser()
    parser.generate_test_controller("Bank")

    """

    def __init__(self):
        self.loaded = False
        self.load_data()

    def load_data(self, url=os.getenv('SWAGGER_URL')):
        self.url = url
        try:
            response = requests.get(url)
            self.__json_data = response.json()
            self.__paths = self.__json_data['paths']
            self.__components = self.__json_data['components']['schemas']
            self.loaded = True
        except Exception as e:
            print(f'Error: {e}')

    def get_all_tags(self):
        result = set()
        for path_data in self.__paths.values():
            for data in path_data.values():
                [tag] = data['tags']
                result.add(tag)
        return list(result)

    def __get_request_body(self, method_data):
        try:
            return method_data['requestBody']['content']['application/json']['schema']['$ref']
        except KeyError:
            return None

    def __get_scheme_by_ref(self, ref):
        key = ref.split('/')[-1]
        schema = self.__components[key]
        body = {}

        if 'properties' not in schema:
            return schema

        for key, value in schema['properties'].items():
            if '$ref' in value:
                body[key] = self.__get_scheme_by_ref(value['$ref'])
            elif 'items' in value and '$ref' in value['items']:
                data = self.__get_scheme_by_ref(value['items']['$ref'])
                body[key] = [data]
            else:
                body[key] = value

        return body

    def __set_default_values(self, value):
        if 'type' in value:
            type_ = value['type']
            if type_ == 'string':
                if 'enum' in value:
                    return value['enum'][0]
                if 'format' in value:
                    if value['format'] == 'date-time':
                        return '2024-01-01T00:00:00Z'
                return 'string'
            elif type_ == 'integer' or type_ == 'number':
                return 0
            elif type_ == 'boolean':
                return True
            elif type_ == 'array':
                return [value['items']['type']]
            else:
                return type_

        body = {}

        for key, val in value.items():
            if isinstance(val, dict):
                body[key] = self.__set_default_values(val)
            elif isinstance(val, list):
                body[key] = [self.__set_default_values(v) for v in val]
        return body

    def __get_request_params(self, method_data):
        parameters = method_data.get('parameters', [])
        result = []

        for parameter in parameters:
            if parameter['in'] == 'path':
                continue

            if "$ref" in parameter['schema']:
                schema = self.__get_scheme_by_ref(parameter['schema']['$ref'])
                val = self.__set_default_values(schema)
                result.append({
                    "name": parameter['name'],
                    "type": val,
                })
                continue

            result.append({
                "name": parameter['name'],
                "type": parameter['schema']['type'],
            })
        return result

    def __filter_paths_by_tag(self, tag):
        result = []
        for path, path_data in self.__paths.items():
            for method, method_data in path_data.items():
                if tag not in method_data['tags']:
                    continue

                requestBody = self.__get_request_body(method_data)
                parameters = self.__get_request_params(method_data)
                repl = r'/api/v{version}/'
                endopoint = re.sub(repl, '', path, flags=re.IGNORECASE)
                result.append({
                    'endpoint': endopoint,
                    'method': method,
                    'parameters': parameters,
                    'requestBody': requestBody,
                })
        return result

    @staticmethod
    def __create_url_string(item):
        parameters = item['parameters']
        endpoint = item['endpoint']
        if not parameters:
            return endpoint

        result = [f'{p["name"]}={p["type"]}' for p in parameters]
        params = '&'.join(result)
        return f'{endpoint}?{params}'

    def __get_table_details(self, tag):
        result = self.__filter_paths_by_tag(tag)
        details = []

        for item in result:
            url = self.__create_url_string(item)
            method = item['method']
            filename = ""
            json_body = ""

            if item['requestBody']:
                body = self.__get_scheme_by_ref(item['requestBody'])
                json_body = self.__set_default_values(body)
                filename = self.__generate_json_file_name(item)

            details.append({
                "endpoint": item['endpoint'],
                "url": url,
                "method": method,
                "body": json_body,
                "filename": filename,
            })
        return details

    @staticmethod
    def __save_file(file_name, data):
        with open(file_name, 'w') as file:
            json.dump(data, file, indent=3)

    @staticmethod
    def __convert_endpoint(endpoint):
        return re.sub(r'/{(.*?)\}', r'\1', endpoint)

    @staticmethod
    def __get_controller_suffix(method):
        dc = {'post': 'Create', 'put': 'Update'}
        return dc.get(method.lower(), method.capitalize())

    @staticmethod
    def __get_filename_suffix(m):
        dc = {'post': 'Add', 'put': 'Update'}
        return dc.get(m.lower(), '')

    def __generate_json_file_name(self, item):
        method = item['method']
        ep = item['endpoint']
        suffix = self.__get_filename_suffix(method)
        cp = self.__convert_endpoint(ep)
        return f'{suffix}{cp.split('/')[-1]}.json'

    def __generate_csharp_test_method(self, item):
        url = item['url']
        http_method = item['method'].capitalize()
        endpoint = self.__convert_endpoint(item['endpoint'])
        endpoint = endpoint.split('/')[-1]
        filename = item['filename']
        body = item['body']
        suffix = self.__get_controller_suffix(http_method)

        request = f'var {endpoint}Request = File.ReadAllText($"{{_filePath}}{
            filename}");'
        result = f"""var result = await _httpClientService.{
            http_method}Async($"{{_apiUrl}}/{url}",{body and f' {endpoint}Request,'} _token);"""

        def request_body():
            if body:
                return f"""{request}
            {result}"""
            return result

        method = f"""
        [Fact]
        public async Task {"" if endpoint.startswith('Get') else suffix}{endpoint}()
        {{
            {request_body()}
            var responseString = await result.Content.ReadAsStringAsync();
            var actualResult = JsonConvert.DeserializeObject<dynamic>(responseString);
            Assert.True(result.StatusCode == System.Net.HttpStatusCode.OK);
        }}
        """

        return method

    def generate_test_controller(self, tag):
        if not self.loaded:
            print('Data not loaded')
            return

        self.tag = tag
        os.makedirs(f'Data/{self.tag}/RequestJson', exist_ok=True)

        details = self.__get_table_details(self.tag)

        if not details:
            print(f'No data found for {self.tag}')
            os.rmdir(f'Data/{self.tag}/RequestJson')
            os.rmdir(f'Data/{self.tag}')
            return

        methods = []
        for detail in details:
            body = detail['body']

            if body:
                file_path = f'Data/{self.tag}/RequestJson/{detail['filename']}'
                self.__save_file(file_path, body)

            method = self.__generate_csharp_test_method(detail)
            methods.append(method)

        with open(f'Data/{self.tag}/{self.tag}ControllerTests.cs', 'w') as file:
            file.write(f"""using Newtonsoft.Json;
using PropVivo.API.IntegrationTests.Helper;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Threading.Tasks;

namespace PropVivo.API.IntegrationTests.{self.tag}
{'{'}
    public class {self.tag}ControllerTests
    {'{'}
        private readonly IHttpClientService _httpClientService;
        private string _apiUrl = "{os.getenv('API_URL')}";
        private string _filePath = string.Format(CultureInfo.CurrentCulture, string.Format("..\\\\..\\\\..\\\\{{0}}\\\\RequestJson\\\\", "{self.tag}"));
        private string _token = "{os.getenv('TOKEN')}";
        public {self.tag}ControllerTests()
        {{
            this._httpClientService = new HttpClientService();
        }}
""")
            for method in methods:
                file.write(method)
            file.write("""
    }
}
""")


if __name__ == '__main__':
    parser = SwaggerParser()

    tables = ["FiscalYear"]
    # tables = parser.get_all_tags()
    for tag in tqdm(tables):
        try:
            parser.generate_test_controller(tag)
        except Exception as e:
            print(f'Error: {tag} {e}')
            break
