from preql.sql import mysql
from . import pql_objects as objects
from . import sql
from .pql_types import T, dp_type
from .exceptions import Signal
# from .interp_common import dy, State, assert_type, new_value_instance, evaluate, call_pql_func

@dp_type
def _cast(state, inst_type, target_type, inst):
    raise Signal.make(T.TypeError, state, None, f"Cast not implemented for {inst_type}->{target_type}")

@dp_type
def _cast(state, inst_type: T.list, target_type: T.list, inst):
    if inst is objects.EmptyList:
        return inst.replace(type=target_type)

    if (inst_type.elem <= target_type.elem):
        return inst

    value = inst.get_column('value')
    elem = _cast(state, value.type, target_type.elem, value)
    code = sql.Select(target_type, inst.code, [sql.ColumnAlias(elem.code, 'value')])
    return inst.replace(code=code, type=T.list[elem.type])


@dp_type
def _cast(state, inst_type: T.aggregate, target_type: T.list, inst):
    res = _cast(state, inst_type.elem, target_type.elem, inst.elem)
    return objects.aggregate(res)   # ??

@dp_type
def _cast(state, inst_type: T.table, target_type: T.list, inst):
    t = inst.type
    if len(t.elems) != 1:
        raise Signal.make(T.TypeError, state, None, f"Cannot cast {inst_type} to {target_type}. Too many columns")
    if not (inst_type.elem <= target_type.elem):
        raise Signal.make(T.TypeError, state, None, f"Cannot cast {inst_type} to {target_type}. Elements not matching")

    (elem_name, elem_type) ,= inst_type.elems.items()
    code = sql.Select(T.list[elem_type], inst.code, [sql.ColumnAlias(sql.Name(elem_type, elem_name), 'value')])

    return objects.ListInstance.make(code, T.list[elem_type], [inst])

@dp_type
def _cast(state, inst_type: T.t_id, target_type: T.int, inst):
    return inst.replace(type=T.int)

@dp_type
def _cast(state, inst_type: T.union[T.float, T.bool], target_type: T.int, inst):
    if state.db.target is mysql:
        t = "signed integer"
    else:
        t = "int"
    code = sql.Cast(T.int, t, inst.code)
    return objects.Instance.make(code, T.int, [inst])

@dp_type
def _cast(state, inst_type: T.union[T.int, T.bool], target_type: T.float, inst):
    code = sql.Cast(T.float, "float", inst.code)
    return objects.Instance.make(code, T.float, [inst])

# @dp_type
# def _cast(state, inst_type: T.string, target_type: T.int, inst):
#     # TODO error on bad string?
#     code = sql.Cast(T.int, "int", inst.code)
#     return objects.Instance.make(code, T.int, [inst])

@dp_type
def _cast(state, inst_type: T.primitive, target_type: T.string, inst):
    code = sql.Cast(T.string, "varchar", inst.code)
    return objects.Instance.make(code, T.string, [inst])

@dp_type
def _cast(state, inst_type: T.vectorized, target_type: T.any, inst):
    x = objects.unvectorized(inst)
    return _cast(state, x.type, target_type, x)

@dp_type
def _cast(state, inst_type: T.t_relation, target_type: T.t_id, inst):
    # TODO verify same table? same type?
    return inst.replace(type=target_type)

