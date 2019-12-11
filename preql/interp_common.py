from contextlib import contextmanager

from .dispatchy import Dispatchy
from .exceptions import pql_NameNotFound, pql_TypeError, Meta

from . import pql_ast as ast
from . import pql_objects as objects
from . import pql_types as types
from . import sql

dy = Dispatchy()

# Define common dispatch functions
@dy
def simplify():
    raise NotImplementedError()



class State:
    def __init__(self, db, fmt, ns=None):
        self.db = db
        self.fmt = fmt

        # self.ns = [_initial_namespace()]
        self.ns = ns or [{}]
        self.tick = 0

    def get_var(self, name):
        for scope in reversed(self.ns):
            if name in scope:
                return scope[name]

        # try:
        #     meta = meta_from_token(name)
        #     meta.parent = meta
        # except AttributeError:
        #     meta = None

        raise pql_NameNotFound(name.meta, str(name))

    def set_var(self, name, value):
        assert not isinstance(value, ast.Name)
        self.ns[-1][name] = value

    def get_all_vars(self):
        d = {}
        for scope in self.ns:
            d.update(scope) # Overwrite upper scopes
        return d

    def push_scope(self):
        self.ns.append({})

    def pop_scope(self):
        return self.ns.pop()


    def __copy__(self):
        s = State(self.db, self.fmt)
        s.ns = [dict(n) for n in self.ns]
        s.tick = self.tick
        return s

    @contextmanager
    def use_scope(self, scope: dict):
        x = len(self.ns)
        self.ns.append(scope)
        try:
            yield
        finally:
            self.ns.pop()
            assert x == len(self.ns)


def get_alias(state: State, obj):
    if isinstance(obj, objects.TableInstance):
        return get_alias(state, obj.type.name)

    state.tick += 1
    return obj + str(state.tick)


def assert_type(t, type_, msg):
    concrete = t.concrete_type()
    if not isinstance(concrete, type_):
        raise pql_TypeError(msg % (type_.__name__, concrete))


def sql_repr(x):
    if x is None:
        return sql.null

    t = types.primitives_by_pytype[type(x)]
    return sql.Primitive(t, repr(x))


# def meta_from_token(tok):
#     return Meta(
#         '', # TODO better exceptions
#         tok.pos_in_stream,
#         tok.line,
#         tok.column,
#         tok.end_pos,
#         tok.end_line,
#         tok.end_column,
#     )

