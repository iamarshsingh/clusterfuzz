# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Manage job types."""

from builtins import str
import six

from google.cloud import ndb

from base import tasks
from datastore import data_handler
from datastore import data_types
from datastore import ndb_utils
from fuzzing import fuzzer_selection
from handlers import base_handler
from libs import filters
from libs import form
from libs import gcs
from libs import handler
from libs import helpers
from libs.query import datastore_query

PAGE_SIZE = 20
MORE_LIMIT = 100 - PAGE_SIZE  # exactly 5 pages

KEYWORD_FILTERS = [
    filters.String('name', 'name'),
]

FILTERS = [
    filters.Keyword(KEYWORD_FILTERS, 'keywords', 'q'),
]


def get_queues():
  """Return list of task queues."""
  queues = []
  for name, display_name in six.iteritems(tasks.TASK_QUEUE_DISPLAY_NAMES):
    queue = {
        'name': name,
        'display_name': display_name,
    }
    queues.append(queue)

  queues.sort(key=lambda q: q['display_name'])
  return queues

def get_results(this):
  """Get results for the jobs page."""
  print("pressed")
  params = {k: v for k, v in this.request.iterparams()}
  print(params)
  query = datastore_query.Query(data_types.Job)
  query.order('name', is_desc=False)
  page = helpers.cast(
      this.request.get('page') or 1, int, "'page' is not an int.")
  #print("page: ", page)
  filters.add(query, params, FILTERS)
  items, total_pages, total_items, has_more = query.fetch_page(
      page=page, page_size=PAGE_SIZE, projection=None, more_limit=MORE_LIMIT)

  #print(items, total_pages, total_items, has_more)
  # jobss = list(data_types.Job.query().order(data_types.Job.name))
  #print("jobss: ", len(jobss))
  result = {
      'hasMore': has_more,
      'items': items,
      'page': page,
      'pageSize': PAGE_SIZE,
      'totalItems': total_items,
      'totalPages': total_pages,
  }

  return result, params


class Handler(base_handler.Handler):
  """View job handler."""

  @handler.check_user_access(need_privileged_access=True)
  @handler.get(handler.HTML)
  def get(self):
    """Handle a get request."""
    helpers.log('Jobs', helpers.VIEW_OPERATION)
    
    templates = list(data_types.JobTemplate.query().order(
        data_types.JobTemplate.name))
    queues = get_queues()

    result, params = get_results(self)
    
    self.render('jobs.html', {
        'result': result,
        'templates': templates,
        'fieldValues': {
            'csrf_token': form.generate_csrf_token(),
            'queues': queues,
            'update_job_url': '/update-job',
            'update_job_template_url': '/update-job-template',
            'upload_info': gcs.prepare_blob_upload()._asdict(),
        },
        'params': params,
    })


class UpdateJob(base_handler.GcsUploadHandler):
  """Update job handler."""

  @handler.check_user_access(need_privileged_access=True)
  @handler.require_csrf_token
  def post(self):
    """Handle a post request."""
    name = self.request.get('name')
    if not name:
      raise helpers.EarlyExitException('Please give this job a name!', 400)

    if not data_types.Job.VALID_NAME_REGEX.match(name):
      raise helpers.EarlyExitException(
          'Job name can only contain letters, numbers, dashes and underscores.',
          400)

    templates = self.request.get('templates', '').splitlines()
    for template in templates:
      if not data_types.JobTemplate.query(
          data_types.JobTemplate.name == template).get():
        raise helpers.EarlyExitException('Invalid template name(s) specified.',
                                         400)

    new_platform = self.request.get('platform')
    if not new_platform or new_platform == 'undefined':
      raise helpers.EarlyExitException('No platform provided for job.', 400)

    description = self.request.get('description', '')
    environment_string = self.request.get('environment_string', '')
    previous_custom_binary_revision = 0

    job = data_types.Job.query(data_types.Job.name == name).get()
    recreate_fuzzer_mappings = False
    if not job:
      job = data_types.Job()
    else:
      previous_custom_binary_revision = job.custom_binary_revision
      if previous_custom_binary_revision is None:
        previous_custom_binary_revision = 0
      if new_platform != job.platform:
        # The rare case of modifying a job's platform causes many problems with
        # task selection. If a job is leased from the old queue, the task will
        # be recreated in the correct queue at lease time. Fuzzer mappings must
        # be purged and recreated, since they depend on the job's platform.
        recreate_fuzzer_mappings = True

    job.name = name
    job.platform = new_platform
    job.description = description
    job.environment_string = environment_string
    job.templates = templates

    blob_info = self.get_upload()
    if blob_info:
      job.custom_binary_key = str(blob_info.key())
      job.custom_binary_filename = blob_info.filename
      job.custom_binary_revision = previous_custom_binary_revision + 1

    if job.custom_binary_key and 'CUSTOM_BINARY' not in job.environment_string:
      job.environment_string += '\nCUSTOM_BINARY = True'

    job.put()

    if recreate_fuzzer_mappings:
      fuzzer_selection.update_platform_for_job(name, new_platform)

    # pylint: disable=unexpected-keyword-arg
    _ = data_handler.get_all_job_type_names(__memoize_force__=True)

    helpers.log('Job created %s' % name, helpers.MODIFY_OPERATION)
    template_values = {
        'title':
            'Success',
        'message': ('Job %s is successfully updated. '
                    'Redirecting back to jobs page...') % name,
        'redirect_url':
            '/jobs',
    }
    self.render('message.html', template_values)


class UpdateJobTemplate(base_handler.Handler):
  """Update job template handler."""

  @handler.check_user_access(need_privileged_access=True)
  @handler.require_csrf_token
  @handler.post(handler.FORM, handler.HTML)
  def post(self):
    """Handle a post request."""
    name = self.request.get('name')
    if not name:
      raise helpers.EarlyExitException('Please give this template a name!', 400)

    if not data_types.Job.VALID_NAME_REGEX.match(name):
      raise helpers.EarlyExitException(
          'Template name can only contain letters, numbers, dashes and '
          'underscores.', 400)

    environment_string = self.request.get('environment_string')
    if not environment_string:
      raise helpers.EarlyExitException(
          'No environment string provided for job template.', 400)

    template = data_types.JobTemplate.query(
        data_types.JobTemplate.name == name).get()
    if not template:
      template = data_types.JobTemplate()

    template.name = name
    template.environment_string = environment_string
    template.put()

    helpers.log('Template created %s' % name, helpers.MODIFY_OPERATION)

    template_values = {
        'title':
            'Success',
        'message': ('Template %s is successfully updated. '
                    'Redirecting back to jobs page...') % name,
        'redirect_url':
            '/jobs',
    }
    self.render('message.html', template_values)


class DeleteJobHandler(base_handler.Handler):
  """Delete job handler."""

  @handler.check_user_access(need_privileged_access=True)
  @handler.post(handler.JSON, handler.JSON)
  @handler.require_csrf_token
  def post(self):
    """Handle a post request."""
    key = helpers.get_integer_key(self.request)
    job = ndb.Key(data_types.Job, key).get()
    if not job:
      raise helpers.EarlyExitException('Job not found.', 400)

    # Delete from fuzzers' jobs' list.
    for fuzzer in ndb_utils.get_all_from_model(data_types.Fuzzer):
      if job.name in fuzzer.jobs:
        fuzzer.jobs.remove(job.name)
        fuzzer.put()

    # Delete associated fuzzer-job mapping(s).
    query = data_types.FuzzerJob.query()
    query = query.filter(data_types.FuzzerJob.job == job.name)
    for mapping in ndb_utils.get_all_from_query(query):
      mapping.key.delete()

    # Delete job.
    job.key.delete()

    helpers.log('Deleted job %s' % job.name, helpers.MODIFY_OPERATION)
    self.redirect('/jobs')
    
class JsonHandler(base_handler.Handler):
  """Handler that gets the testcase list when user clicks on next page."""

  @handler.check_user_access(need_privileged_access=True)
  @handler.post(handler.JSON, handler.JSON)
  def post(self):
    """Get and render the testcase list in JSON."""
    result, _ = get_results(self)
    self.render_json(result)

