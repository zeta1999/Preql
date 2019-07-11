from .utils import dataclass, Dataclass, Context
from .utils import dataclass, make_define_decorator, field
from . import ast_classes as ast
from . import sql
from .exceptions import PreqlError_Attribute


class Object(Dataclass):
    tuple_width = 1

    def from_sql_tuple(self, tup):
        obj ,= tup
        return obj

    def from_sql_tuples(self, tuples):
        row ,= tuples
        return type(self)(self.from_sql_tuple(row))

pql_object = make_define_decorator(Object)


class Function(Object):
    def invoked_by_user(self):
        pass    # XXX More complicated than that?

class Null(Object):
    def repr(self, query_engine):
        return 'null'

    def to_sql(self):
        return sql.null

null = Null()

class Primitive(Object):
    value = None

    def repr(self, query_engine):
        return repr(self.value)

    def to_sql(self):
        return sql.Primitive(type(self), self.repr(None))

@pql_object
class Integer(Primitive):
    value: int

@pql_object
class Bool(Primitive):
    value: bool

@pql_object
class Float(Primitive):
    value: float

@pql_object
class String(Primitive):
    value: str

    def repr(self, sql_engine):
        return '"%s"' % self.value

@pql_object
class Row(Object):
    table: object
    attrs: dict

    def repr(self):
        attrs = ['%s: %r' % kv for kv in self.attrs.items()]
        return '{' + ', '.join(attrs) +'}'

    def __repr__(self):
        return self.repr()

    def getattr(self, attr):
        return self.attrs[attr]



@pql_object
class Table(Object):
    """
    All tables (either real or ephemeral) must provide the following interface:
        - List of columns for PQL, that are available as attributes
        - List of columns for SQL, that will be used for query (unstructured, ofc)

    Table identity is meaningful. References to the same table, under the same context,
    should return the same table.

    base_table exists so that nested autojoins will know which table to join against.
    """



    @property
    def base_table(self):   # For autojoins
        return self

    def getattr(self, name):
        try:
            return self.get_column(name)
        except PreqlError_Attribute:
            pass

        if name == 'count':
            # TODO table should have a method dict
            return CountTable(self)
        elif name == 'limit':
            # TODO table should have a method dict
            return LimitTable(self)
        elif name == 'offset':
            # TODO table should have a method dict
            return OffsetTable(self)
        elif name == 'order':
            # TODO table should have a method dict
            return OrderTable(self)
        
        raise PreqlError_Attribute(self.name, name)

    def get_column(self, name):
        try:
            col = self._columns[name]
        except KeyError:
            raise PreqlError_Attribute(self.name, name)

        col.invoked_by_user()
        return col

    def _repr_table(self, query_engine, name):
        preview_limit = 3
        count = self.count(query_engine).value
        rows = self.query(query_engine, preview_limit)
        rows = [r.repr() for r in rows]
        if count > preview_limit:
            rows.append('... (%d more)' % (count - preview_limit))

        table_line = '<Table'
        if name:
            table_line += ':' + name
        table_line += ' count=' + str(count) + '>'
        lines = [table_line] + ['\t - %s' % (r,) for r in rows]
        return '\n'.join(lines)

    def count(self, query_engine):
        return query_engine.query( sql.Select(Integer(), self.to_sql(), fields=[sql.CountField(sql.Primitive(None, '*'))]) )

    def to_sql(self):
        raise NotImplementedError()

    def query(self, query_engine, limit=None):
        s = sql.Select(self, self.to_sql(), [sql.Primitive(None, '*')], limit=sql.Primitive(int, str(limit)) if limit!=None else None)
        return query_engine.query(s)


    def cols_by_type(self, type_):
        return {name: c.col for name, c in self._columns.items()
                if isinstance(c.type, type_)}

    def from_sql_tuple(self, tup):
        items = {}
        for name, col in self.sql_projection.items():
            subset = tup[:col.tuple_width]
            items[name] = col.from_sql_tuple(subset)
            tup = tup[col.tuple_width:]

        assert not tup, (tup, self.sql_projection)

        return Row(self, items)

    def from_sql_tuples(self, tuples):
        return [self.from_sql_tuple(row) for row in tuples]

    @property
    def sql_projection(self):
        return {name:c for name, c in self._columns.items() 
                if not isinstance(c.type, ast.BackRefType) or c.invoked}

    @property
    def tuple_width(self):
        return len(self.sql_projection)

    def repr(self, query_engine):
        return self._repr_table(query_engine, None)

    def __repr__(self):
        assert False
        return '%s:%s { %s }' % (type(self).__name__, self.name, self._columns)


@pql_object
class StoredTable(Table):
    tabledef: ast.TableDef
    query_state: object
    __columns = None

    def _init(self):
        for c in self.tabledef.columns.values():
            assert isinstance(c, ast.Column), type(c)

    @property
    def _columns(self): # Lazy to avoid self-recursion through columnref.relation
        if self.__columns is None:
            self.__columns = {name:ColumnRef(c, self) for name, c in self.tabledef.columns.items()}
        return self.__columns

    # def repr(self, query_engine):
    #     return self._repr_table(query_engine, self.name)

    def to_sql(self, context=None):
        table_ref = sql.TableRef(self, self.name)
        columns = [sql.ColumnAlias(sql.ColumnRef(c.name), c.sql_alias) for c in self.sql_projection.values()]
        return sql.Select(self, table_ref, columns)

    @property
    def name(self):
        return self.tabledef.name

    def __repr__(self):
        return 'StoredTable(%s)' % self.name


MAKE_ALIAS = iter(range(100000))

@pql_object
class JoinableTable(Table):
    """
    When one of the relations/backrefs in this table is being used explicitely,
    the method add_autojoin is called. The tables provided to add_autojoin will then 
    be included when creating the SQL query.


    """
    tabledef: ast.TableDef
    query_state: object

    table = None
    _joins = None

    def _init(self):
        self._joins = []
        self.table = StoredTable(self.tabledef, self.query_state)

    @property
    def name(self):
        return self.tabledef.name

    @property
    def _columns(self):
        return {name:ColumnRef(c.col, self, c.sql_alias) for name, c in self.table._columns.items()}

    def add_autojoin(self, table, is_fwd, col):
        # if (table, is_fwd) not in self._joins:    # XXX is there a point to this?
        join = (table, is_fwd, col)
        if join not in self._joins:
            self._joins.append(join)

    def to_sql(self, context=None):

        # XXX Do this in add_autojoin?
        table = self.table
        for to_join, is_fwd, col in self._joins:
            table = AutoJoin([table, to_join], is_fwd, col)

        return table.to_sql()

    def __repr__(self):
        return 'JoinableTable(%s)' % self.name


def make_alias(base):
    return base.replace('.', '_') + str(next(MAKE_ALIAS))


class ColumnRefBase:    # XXX Temporary
    pass

@pql_object
class ColumnValueRef(ColumnRefBase):   # The new ColumnRef?
    expr: Object
    sql_alias: str

    def invoked_by_user(self):
        return self.expr.invoked_by_user()

    @property
    def type(self):
        return self.expr.type
    
    def to_sql(self):
        return sql.ColumnRef(self.sql_alias)

    @property
    def name(self):
        return self.expr.name

    def getattr(self, attr):
        col = self.expr.getattr(attr)
        # import pdb
        # pdb.set_trace()
        return col
        # new_col = ColumnRef(col.col, col.table, self.sql_alias, col.relation, col.name)
        # new_col.invoked_by_user()
        # return new_col

    @property
    def invoked(self):
        return self.expr.invoked


@pql_object
class ColumnRef(ColumnRefBase):   # TODO proper hierarchy
    col: ast.Column
    table: Table
    sql_alias: str = None
    pql_name: str = None

    _relation = None
    backref = None
    invoked = False
    joined = False  # XXX how is it different than invoked?

    def _init(self):
        if self.sql_alias is None:
            self._init_var('sql_alias', make_alias(self.col.name))  # XXX ugly but that's the best Python has to offer

    
    @property
    def relation(self):
        assert isinstance(self.type, ast.RelationalType)
        if self._relation is None:
            self._relation = self.table.query_state.get_table(id(self), self.type.table_name)
        return self._relation

    def invoked_by_user(self):
        if isinstance(self.type, ast.BackRefType):
            if not self.backref:
                backref = self.table.query_state.get_table(id(self), self.col.table.name) # XXX kinda awkward
                self.table.add_autojoin(backref, False, self)
                self.backref = backref
                self.invoked = True
            assert self.invoked

    def to_sql(self):
        if isinstance(self.type, ast.BackRefType):
            assert self.backref and self.invoked
            # TODO include all fields in sql?
            return ( self.backref.get_column('id').to_sql() )
        
        if self.joined:
            assert isinstance(self.type, ast.RelationalType)
            col_refs = [c.to_sql() for c in self.relation.sql_projection.values()]
            return sql.ColumnRefs(col_refs)

        return sql.ColumnRef(self.sql_alias)

    @property
    def name(self):
        return self.pql_name or self.col.name

    @property
    def type(self):
        return self.col.type

    def getattr(self, name):
        if isinstance(self.type, ast.BackRefType):
            assert (self.backref, False, self) in self.table._joins
            col = self.backref.get_column(name)
        else:
            assert isinstance(self.type, ast.RelationalType)
            self.table.add_autojoin(self.relation, True, self)
            self.joined = True
            col = self.relation.get_column(name)

        # TODO Store ColumnRef for correct re-use?
        new_col = ColumnRef(col.col, col.table, col.sql_alias, self.name + '.' + col.name)
        new_col.invoked_by_user()
        return new_col

    def from_sql_tuple(self, tup):
        if isinstance(self.type, ast.RelationalType):
            # TODO return object instead of id
            pass

        res = super().from_sql_tuple(tup)
        if isinstance(res, list):
            res = [self.from_sql_tuple([elem]) for elem in res]
        return res


@pql_object
class TableMethod(Function):
    table: Table

@pql_object
class CountTable(TableMethod):

    def call(self, query_engine, args, named_args):
        assert not args, args
        assert not named_args
        return self.table.count(query_engine)

    def repr(self, query_engine):
        return f'<CountTable function>'

@pql_object
class LimitTable(TableMethod):
    def call(self, query_engine, args, named_args):
        assert not named_args
        limit ,= args
        return Query(self.table, limit=limit)

class OffsetTable(TableMethod):
    def call(self, query_engine, args, named_args):
        assert not named_args
        offset ,= args
        return Query(self.table, offset=offset)


@pql_object
class OrderTable(TableMethod):
    def call(self, query_engine, args, named_args):
        assert not named_args
        return Query(self.table, order=args)



@pql_object
class CountField(Function): # TODO not exactly function
    obj: ColumnRefBase
    type = Integer()

    def to_sql(self):
        return sql.CountField( self.obj.to_sql() )
    
    @property
    def name(self):
        return f'count_{self.obj.name}'

@pql_object
class LimitField(Function): # TODO not exactly function
    obj: ColumnRefBase
    limit: Integer

    def to_sql(self):
        return sql.LimitField(self.obj.to_sql(), self.limit.value)
    
    @property
    def name(self):
        return f'limit_{self.obj.name}'

    @property
    def type(self):
        return self.obj.type

    def from_sql_tuple(self, tup):
        raise NotImplementedError()
    

@pql_object
class Round(Function):  # TODO not exactly function
    obj: Object
    type = Float

    def to_sql(self):
        return sql.RoundField( self.obj.to_sql() )

    @property
    def name(self):
        return f'round_{self.obj.name}'


@pql_object
class SqlFunction(Function):
    f: object

    def call(self, query_engine, args, named_args):
        return self.f(*args, **named_args)

@pql_object
class UserFunction(Function):
    funcdef: ast.FunctionDef

    # def call(self, query_engine, args):
    #     return self

def safezip(*args):
    assert len({len(a) for a in args}) == 1
    return zip(*args)

@pql_object
class Array(Primitive):
    expr: Object
    
    @property
    def name(self):
        return self.expr.name

    def invoked_by_user(self):
        self.expr.invoked_by_user()

    def to_sql(self):
        return sql.MakeArray(self.expr.to_sql())

    @property
    def type(self):
        return ast.ArrayType(self.expr.type)





@pql_object
class Query(Table):
    table: Table
    conds: list = None
    fields: list = None
    agg_fields: list = None
    order: list = None
    offset: Object = None
    limit: Object = None

    name = '<Query Object>'

    aliases = None
    _fields = None
    _agg_fields = None

    def _init(self):
        for f in self.fields or []:
            if isinstance(f.type, ast.BackRefType): # or isinstance(f.type, Integer): # XXX Do correct type check
                raise TypeError('Misplaced column "%s". Aggregated columns must appear after the aggregation operator "=>" ' % f.name)

        self._fields = self.fields or list(self.table._columns.values())
        self._agg_fields = [Array(f) if not isinstance(f.type, Integer) else f
                            for f in self.agg_fields or []]  # TODO By expr type (if array or not)

        for f in self._fields + self._agg_fields:
            alias = getattr(f, 'sql_alias', None)
            if alias is None:
                f.sql_alias = make_alias(f.name)

    @property
    def _columns(self):
        return {f.name: ColumnValueRef(f, f.sql_alias) for f in self._fields + self._agg_fields}

    def to_sql(self, context=None):
        # TODO assert types?
        fields = [(f.to_sql(), f.sql_alias) for f in self._fields
                  if not isinstance(f.type, ast.BackRefType) or f.invoked ]

        agg_fields = [(f.to_sql(), f.sql_alias) for f in self._agg_fields]

        # Alias all fields, except composite fields (due to foreign key expansion)
        # This works under the assumption that the columns inside don't change, but only propagate up as they are
        # XXX Does this hold for sophisiticated projections? Columns transformations?
        sql_fields = [sql.ColumnAlias(f, a)
                      if not isinstance(f, sql.ColumnRefs)
                      else f
                      for f, a in fields + agg_fields]

        return sql.Select(
            type = self,
            table = self.table.to_sql(),
            fields = sql_fields,
            conds = [c.to_sql() for c in self.conds or []],
            group_by = [f for f,a in fields] if agg_fields else [],
            order = [o.to_sql() for o in self.order or []],
            offset = self.offset.to_sql() if self.offset else None,
            limit = self.limit.to_sql() if self.limit else None,
        )

    def from_sql_tuple(self, tup):
        fields = [f for f in self._fields + self._agg_fields
                  if not isinstance(f.type, ast.BackRefType) or f.invoked ]
        tup = [v if not isinstance(f.type, ast.ArrayType)
                 else f.from_sql_tuple([sql.MakeArray(None).import_value(v)])  # XXX hackish
                 for f, v in safezip(fields, tup)]
        return super().from_sql_tuple(tup)

    def repr(self, query_engine):
        return self._repr_table(query_engine, None)



def is_null(expr):
    return sql.Compare('is', [expr, sql.null])
def is_not_null(expr):
    return sql.Compare('is not', [expr, sql.null])

@pql_object
class Contains(Object):
    op: str
    exprs: list

    type = ast.BoolType()

    def to_sql(self):
        assert len(self.exprs) == 2
        exprs = [e.to_sql() for e in self.exprs]
        return sql.Contains(self.op, exprs)


@pql_object
class Compare(Object): # TODO Op? Function?
    op: str
    exprs: list

    type = ast.BoolType()

    def to_sql(self):
        assert len(self.exprs) == 2  # XXX Otherwise NULL handling needs to be smarter
        nulls = any(expr is null for expr in self.exprs)
        if nulls:
            op = {
                '=': 'is',
                '!=': 'is not',
            }[self.op]
        else:
            op = self.op

        exprs = [e.to_sql() for e in self.exprs]
        compare = sql.Compare(op, exprs)

        if op == '!=':
            # XXX Very hacky!!!
            # Exists to override SQL behavior where (a != b) is NULL if one of them is NULL

            assert not nulls
            assert len(exprs) == 2
            e1, e2 = exprs
            compare = sql.Arith('OR', [
                compare,
                sql.Arith('AND', [is_null(e1), is_not_null(e2)]),
                sql.Arith('AND', [is_null(e2), is_not_null(e1)]),
              ])

        return compare

@pql_object
class Arith(Object): # TODO Op? Function?
    op: str
    exprs: list

    def to_sql(self):
        return sql.Arith(self.op, [e.to_sql() for e in self.exprs])

@pql_object
class Neg(Object): # TODO Op? Function?
    expr: Object

    def to_sql(self):
        return sql.Neg(self.expr.to_sql())

@pql_object
class Desc(Object): # TODO Op? Function?
    expr: Object

    def to_sql(self):
        return sql.Desc(self.expr.to_sql())

@pql_object
class NamedExpr(Object):   # XXX this is bad but I'm lazy
    _name: str
    expr: Object

    def to_sql(self):
        return self.expr.to_sql()

    @property
    def name(self):
        if self._name:
            return self._name
        try:
            return self.expr.name
        except AttributeError:
            self._init_var('_name', type(self.expr).__name__ + str(next(MAKE_ALIAS)))  # XXX ugly but that's the best Python has to offer
            return self._name

    @property
    def type(self):
        return self.expr.type

    @property
    def tuple_width(self):
        return self.expr.tuple_width

    def from_sql_tuple(self, tup):
        return self.expr.from_sql_tuple(tup)

    def invoked_by_user(self):
        return self.expr.invoked_by_user()

    @property
    def invoked(self):
        return self.expr.invoked


@pql_object
class RowRef(Object):
    table: object
    row_id: int
    _query_engine: object

    def to_sql(self):
        return sql.Primitive(Integer, str(self.row_id)) # XXX type = table?

    def getattr(self, attr):
        if attr == 'id':
            return self.row_id

        # TODO check column exists in table
        # TODO use Pql object instead of constructing sql myself?
        table = self._query_engine.eval_expr(self.table.name, {})
        query = Query(table,
                      conds=[Compare('=', [table._columns['id'], Integer(self.row_id)])],
                      fields=[table._columns[attr]]
                      )
        res ,= query.query(self._query_engine)
        return res.getattr(attr)

    def repr(self):
        return 'RowRef(table=%r, id=%d)' % (self.table, self.row_id)


@pql_object
class TableVariable(Table):
    name: str
    table: Table

    @property
    def _columns(self):
        return self.table._columns

    def to_sql(self):
        return self.table.to_sql()
    

@pql_object
class TableField(Table):
    table: Table
    type = Table

    @property
    def _columns(self):
        return self.table._columns

    @property
    def name(self):
        return self.table.name

    def to_sql(self):
        return sql.TableField(self, self.name, self.sql_projection)

    def invoked_by_user(self):
        pass

    def getattr(self, name):
        col = super().getattr(name)
        assert not isinstance(col.type, ast.BackRefType)
        # TODO Store ColumnRef for correct re-use?
        return ColumnRef(col.col, col.table, col.sql_alias, self.name + '.' + col.name)


def create_autojoin(*args, **kwargs):
    tables = [TableVariable(k, v) for k, v in kwargs.items()]
    return AutoJoin(tables)

@pql_object
class AutoJoin(Table):
    tables: [Table]
    is_fwd: bool = True
    col: ColumnRefBase = None

    name = '<Autojoin>'

    @property
    def base_table(self):   # For autojoins
        return self.tables[0]

    @property
    def _columns(self):
        return {t.name:TableField(t) for t in self.tables}

    def to_sql(self, context=None):
        tables = self.tables
        assert len(tables) == 2
        if not self.is_fwd:
            tables = list(reversed(tables))    # Necessary distinction for self-reference

        ids =       [list(t.base_table.cols_by_type(ast.IdType).items()) for t in tables]
        relations = [list(t.base_table.cols_by_type(ast.RelationalType).values()) for t in tables]

        ids0, ids1 = ids
        id0 ,= ids0
        id1 ,= ids1
        name1, name2 = list(tables)
        
        assert len(ids) == 2
        assert len(relations) == 2
    
        table1 = id0[1].type.table
        table2 = id1[1].type.table

        col_name = self.col.col.backref
        to_join  = [(name1, c, name2) for c in relations[0] if c.type.table_name == table2 and (self.is_fwd or c.name==col_name)]
        to_join += [(name2, c, name1) for c in relations[1] if c.type.table_name == table1 and (self.is_fwd or c.name==col_name)]

        if len(to_join) == 2 and list(reversed(to_join[1])) == list(to_join[0]):    # TODO XXX ugly hack!! This won't scale, figure out how to prevent this case
            to_join = to_join[:1]

        if len(to_join) > 1:
            raise Exception("More than 1 relation between %s <-> %s" % (table1, table2))

        to_join ,= to_join
        src_table, rel, dst_table = to_join

        tables = [src_table.to_sql(context), dst_table.to_sql(context)]
        if not self.is_fwd:
            tables.reverse()    # XXX hacky

        key_col = src_table.base_table.get_column(rel.name).sql_alias
        dst_id = dst_table.base_table.get_column('id').sql_alias
        conds = [sql.Compare('=', [sql.ColumnRef(key_col), sql.ColumnRef(dst_id)])]
        return sql.Join(self, tables, conds, '' if self.is_fwd else 'left')
