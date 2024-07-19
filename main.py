import requests
import json
import os
import re
from tqdm import tqdm
from dotenv import load_dotenv

load_dotenv()

json_output = None


def get_json_data():
    global json_output
    if json_output:
        return json_output
    url = os.getenv('SWAGGER_URL')
    response = requests.get(url)
    json_output = response.json()
    return json_output


def get_request_body(method_data):
    try:
        return method_data['requestBody']['content']['application/json']['schema']['$ref']
    except KeyError:
        return None


def get_request_params(method_data):
    parameters = method_data.get('parameters')
    result = []
    if not parameters:
        return result

    for parameter in parameters:
        if parameter['in'] == 'path':
            continue

        if "$ref" in parameter['schema']:
            json_output = get_json_data()
            components = json_output['components']['schemas']
            schema = get_scheme_by_ref(components, parameter['schema']['$ref'])
            val = set_default_values(schema)
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


def filter_paths_by_tag(paths, tag):

    result = []
    for path, path_data in paths.items():
        for method, method_data in path_data.items():
            if tag not in method_data['tags']:
                continue

            requestBody = get_request_body(method_data)
            parameters = get_request_params(method_data)
            repl = r'/api/v{version}/'
            endopoint = re.sub(repl, '', path, flags=re.IGNORECASE)
            result.append({
                'endpoint': endopoint,
                'method': method,
                'parameters': parameters,
                'requestBody': requestBody,
            })
    return result


def pf(data):
    print(json.dumps(data, indent=3), '\n')


def get_scheme_by_ref(components, ref):
    key = ref.split('/')[-1]
    schema = components[key]
    body = {}

    if 'properties' not in schema:
        return schema

    for key, value in schema['properties'].items():
        if '$ref' in value:
            body[key] = get_scheme_by_ref(components, value['$ref'])
        elif 'items' in value and '$ref' in value['items']:
            data = get_scheme_by_ref(components, value['items']['$ref'])
            body[key] = [data]
        else:
            body[key] = value

    return body


def create_url_string(item):
    parameters = item['parameters']
    endpoint = item['endpoint']
    if not parameters:
        return endpoint

    result = [f'{p["name"]}={p["type"]}' for p in parameters]
    params = '&'.join(result)
    return f'{endpoint}?{params}'


def save_file(file_name, data):
    with open(file_name, 'w') as file:
        json.dump(data, file, indent=3)


def set_default_values(value):
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
            body[key] = set_default_values(val)
        elif isinstance(val, list):
            body[key] = [set_default_values(v) for v in val]
    return body


def get_table_details(table):
    json_output = get_json_data()
    paths = json_output['paths']
    components = json_output['components']['schemas']

    result = filter_paths_by_tag(paths, table)

    details = []

    for item in result:
        url = create_url_string(item)
        method = item['method']
        json_body = ""

        if item['requestBody']:
            body = get_scheme_by_ref(components, item['requestBody'])
            json_body = set_default_values(body)

        details.append({
            "endpoint": item['endpoint'],
            "url": url,
            "method": method,
            "body": json_body
        })
    return details


def get_controller_suffix(method):
    m = method.lower()
    dc = {
        'post': 'Create',
        'put': 'Update',
    }
    return dc.get(m, m.capitalize())


def generate_json_file_name(item):
    method = item['method']
    ep = item['endpoint']
    suffix = 'Add' if method == 'post' else 'Update' if method == 'put' else ''
    return f'{suffix}{ep.split('/')[-1]}.json'


def generate_csharp_test_methods(data):
    methods = []
    for item in data:
        url = item['url']
        http_method = item['method'].capitalize()
        endpoint = item['endpoint'].split('/')[-1]
        filename = generate_json_file_name(item)
        body = item['body']
        suffix = get_controller_suffix(http_method)

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
        public async Task {suffix}{endpoint}()
        {{
            {request_body()}
            var responseString = await result.Content.ReadAsStringAsync();
            var actualResult = JsonConvert.DeserializeObject<dynamic>(responseString);
            Assert.True(result.StatusCode == System.Net.HttpStatusCode.OK);
        }}
        """
        methods.append(method)
    return methods


def generate_test_controller(table, details):
    os.makedirs(f'Data/{table}/RequestJson', exist_ok=True)

    for detail in details:
        body = detail['body']
        method = detail['method']

        if body:
            fn = generate_json_file_name(detail)
            save_file(f'Data/{table}/RequestJson/{fn}', body)

        methods = generate_csharp_test_methods(details)

        with open(f'Data/{table}/{table}ControllerTests.cs', 'w') as file:
            file.write(f"""using Newtonsoft.Json;
using PropVivo.API.IntegrationTests.Helper;
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Threading.Tasks;

namespace PropVivo.API.IntegrationTests.{table}
{'{'}
    public class {table}ControllerTests
    {'{'}
        private readonly IHttpClientService _httpClientService;
        private string _apiUrl = "{os.getenv('API_URL')}";
        private string _filePath = string.Format(CultureInfo.CurrentCulture, string.Format("..\\\\..\\\\..\\\\{{0}}\\\\RequestJson\\\\", "{table}"));
        private string _token = "{os.getenv('TOKEN')}";
        public {table}ControllerTests()
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


def get_all_tags():
    json_output = get_json_data()
    paths = json_output['paths']

    result = set()
    for path_data in paths.values():
        for data in path_data.values():
            [tag] = data['tags']
            result.add(tag)
    return list(result)


if __name__ == '__main__':
    os.makedirs('Data', exist_ok=True)
    tables = get_all_tags()
    for tag in tqdm(tables):
        try:
            details = get_table_details(tag)
            generate_test_controller(tag, details)
        except Exception as e:
            print(f'Error: {tag}')
            break
