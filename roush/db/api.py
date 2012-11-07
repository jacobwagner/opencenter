# vim: tabstop=4 shiftwidth=4 softtabstop=4
# tab stops are 8.  ^^ this is wrong

import logging
from time import time
from functools import partial

import sqlalchemy

from roush import backends

from roush.db.database import session
from roush.db import exceptions
from roush.db import models

from roush.webapp.ast import AstBuilder, FilterTokenizer

LOG = logging.getLogger(__name__)


def _get_model_object(model):
    return getattr(models, model.capitalize())


def _model_get_all(model):
    field_list = _model_get_columns(model)

    return [dict((c, getattr(r, c))
                 for c in field_list)
            for r in _get_model_object(model).query.all()]


def _model_get_columns(model):
    mo = _get_model_object(model)

    field_list = [c for c in mo.__table__.columns.keys()]

    if hasattr(mo, '_synthesized_fields'):
        field_list += mo._synthesized_fields

    return field_list


def _model_get_schema(model):
    obj = _get_model_object(model)
    cols = obj.__table__.columns

    fields = {}
    for k in cols.keys():
        fields[k] = {}
        fields[k]['type'] = str(cols[k].type)
        if repr(cols[k].type) == 'JsonBlob()':
            fields[k]['type'] = 'JSON'

        if repr(cols[k].type) == 'JsonEntry()':
            fields[k]['type'] = 'JSON_ENTRY'

        fields[k]['primary_key'] = cols[k].primary_key
        fields[k]['unique'] = cols[k].unique or cols[k].primary_key
        fields[k]['updatable'] = True
        fields[k]['required'] = not cols[k].nullable

        if hasattr(obj, '_non_updatable_fields'):
            if k in obj._non_updatable_fields:
                fields[k]['updatable'] = False

        if len(cols[k].foreign_keys) > 0:
            fields[k]['fk'] = list(cols[k].foreign_keys)[0].target_fullname

    if hasattr(obj, '_synthesized_fields'):
        for syn in obj._synthesized_fields:
            fields[syn] = {'type': 'TEXT',
                           'unique': False,
                           'nullable': False,
                           'updatable': False,
                           'primary_key': False}

    return {'schema': fields}


def _model_create(model, fields):
    """Query helper for creating a row

    :param model: name of the table model
    :param fields: dict of columns:values to create
    """
    model_object = _get_model_object(model)
    field_list = [c for c in model_object.__table__.columns.keys()]
    field_list.remove('id')
    r = model_object(**dict((field, fields[field])
                            for field in field_list if field in fields))

    session.add(r)
    try:
        session.commit()
        ret = dict((c, getattr(r, c))
                   for c in r.__table__.columns.keys())
        backends.notify(model.rstrip('s'), 'create', None, ret)
        return ret
    except backends.BackendException as e:
        session.rollback()
        msg = 'backend failure: %s' % str(e)
        raise exceptions.CreateError(msg)
    except sqlalchemy.exc.StatementError as e:
        session.rollback()
        # msg = e.message
        msg = "JSON object must be either type(dict) or type(list) " \
              "not %s" % (e.message)
        raise exceptions.CreateError(msg)
    except sqlalchemy.exc.IntegrityError as e:
        session.rollback()
        msg = "Unable to create %s, duplicate entry" % (model.title())
        raise exceptions.CreateError(message=msg)


def _model_delete_by_id(model, pk_id):
    """Query helper for deleting a node

    :param model: name of the table model
    :param pk_id: id to delete
    """
    r = _get_model_object(model).query.filter_by(id=pk_id).first()
    # We need generate an object hash to pass to the backend notification
    old_obj = None
    if r is not None:
        old_obj = dict((c, getattr(r, c))
                       for c in r.__table__.columns.keys())

    try:
        session.delete(r)
        backends.notify(model.rstrip('s'), 'delete', old_obj, None)
        session.commit()
        return True
    except backends.BackendException as e:
        session.rollback()
        msg = 'backend failure: %s' % str(e)
        raise exc.CreateError(msg)
    except sqlalchemy.orm.exc.UnmappedInstanceError as e:
        session.rollback()
        msg = "%s id does not exist" % (model.title())
        raise exc.IdNotFound(message=msg)
    except sqlalchemy.exc.InvalidRequestError as e:
        session.rollback()
        msg = e.msg
        raise RuntimeError(msg)


def _model_get_by_id(model, pk_id):
    """Query helper for getting a node

    :param model: name of the table model
    :param pk_id: id to delete
    """

    result = _model_get_by_filter(model, {'id': pk_id})

    if len(result) == 0:
        return None

    return result[0]


def _model_get_first_by_filter(model, filters):
    result = _model_get_by_filter(model, filters)
    if len(result):
        return result[0]
    return None


def _model_get_by_filter(model, filters):
    """Query helper that returns a node dict.

    :param filters: dictionary of filters; that are combined with AND
                    to filter the result set.
    """
    filter_options = sqlalchemy.sql.and_(
        * [_get_model_object(model).__table__.columns[k] == v
           for k, v in filters.iteritems()])
    r = _get_model_object(model).query.filter(filter_options)
    if not r:
        result = None
    else:
        result = [dict((c, getattr(res, c))
                       for c in _model_get_columns(model)) for res in r]
    return result


def _model_query(model, query):
    query = '%s: %s' % (model, query)

    builder = AstBuilder(FilterTokenizer(), query)
    result = builder.eval()

    return result


def _model_update_by_id(model, pk_id, fields):
    """Query helper for updating a row

    :param model: name of the table model
    :param pk_id: id to update
    :param pk_id: dict of columns:values to update
    """
    field_list = [c for c in _get_model_object(model).__table__.columns.keys()]

    r = _get_model_object(model).query.filter_by(id=pk_id).first()

    if hasattr(r, '_non_updatable_fields'):
        for d in r._non_updatable_fields:
            field_list.remove(d)

    # We need generate an object hash to pass to the backend notification
    old_obj = None
    if r is not None:
        old_obj = dict((c, getattr(r, c))
                       for c in r.__table__.columns.keys())

    for field in field_list:
        if field in fields:
            r.__setattr__(field, fields[field])
    try:
        ret = dict((c, getattr(r, c))
                   for c in r.__table__.columns.keys())
        backends.notify(model.rstrip('s'), 'update', old_obj, ret)
        session.commit()
        return ret
    except backends.BackendException as e:
        session.rollback()
        msg = 'backend failure: %s' % str(e)
        raise e
    except sqlalchemy.exc.InvalidRequestError as e:
        print "invalid req"
        session.rollback()
        msg = e.msg
        raise RuntimeError(msg)
    except:
        session.rollback()
        raise


# set up the default boilerplate functions, then
# allow overrides after that
for d in dir(models):
    if type(models.Nodes) == type(getattr(models, d)) and d != 'Base':
        model = d.lower()
        sing = model[:-1]

        globals()['%s_get_all' % model] = partial(
            _model_get_all, model)
        globals()['%s_delete_by_id' % sing] = partial(
            _model_delete_by_id, model)
        globals()['%s_get_columns' % sing] = partial(
            _model_get_columns, model)
        globals()['%s_get_first_by_filter' % sing] = partial(
            _model_get_first_by_filter, model)
        globals()['%s_get_by_id' % sing] = partial(
            _model_get_by_id, model)
        globals()['%s_create' % sing] = partial(
            _model_create, model)
        globals()['%s_update_by_id' % sing] = partial(
            _model_update_by_id, model)
        globals()['%s_query' % model] = partial(
            _model_query, model)


def adventures_get_by_node_id(node_id):
    """Query helper that returns a dict of all the adventures
       for a given node_id

    :param node_id: blah blah
    """
    # this is the SQL query we are trying to achieve
    # select adventures.* from adventures join nodes on
    #    (nodes.backend = adventures.backend or adventures.backend = null)
    #    AND (nodes.backend_state = adventures.backend_state
    #         OR adventures.backend_state is null);

    stmt1 = sqlalchemy.sql.or_(
        models.Adventures.backend == models.Nodes.backend,
        models.Adventures.backend == 'null')
    stmt2 = sqlalchemy.sql.or_(
        models.Adventures.backend_state == models.Nodes.backend_state,
        models.Adventures.backend_state == 'null')
    adventure_list = models.Adventures.query.join(
        models.Nodes,
        and_(stmt1, stmt2, Nodes.id == node_id)).all()

    result = [dict((c, getattr(r, c))
                   for c in r.__table__.columns.keys())
              for r in adventure_list]
    return result