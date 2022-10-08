from __future__ import annotations

from copy import copy
import functools
import typing as t
import zlib

import sqlglot
from sqlglot import expressions as exp
from sqlglot.dataframe.sql.column import Column
from sqlglot.dataframe.sql import functions as F
from sqlglot.helper import ensure_list
from sqlglot.dataframe.sql.group import GroupedData
from sqlglot.dataframe.sql.operations import Operation, operation
from sqlglot.dataframe.sql.sanitize import sanitize
from sqlglot.dataframe.sql.readwriter import DataFrameWriter
from sqlglot.dataframe.sql.util import get_tables_from_expression_with_join
from sqlglot.optimizer import optimize as optimize_func
from sqlglot.optimizer.qualify_columns import qualify_columns


if t.TYPE_CHECKING:
    from sqlglot.dataframe.sql.session import SparkSession


class DataFrame:
    def __init__(self, spark: SparkSession, expression: exp.Select, branch_id: str = None, sequence_id: str = None, last_op: t.Optional[Operation] = Operation.INIT, pending_hints: t.List[exp.Expression] = None, **kwargs):
        self.spark = spark
        self.expression = expression
        self.branch_id = branch_id or self.spark._random_branch_id
        self.sequence_id = sequence_id or self.spark._random_sequence_id
        self.last_op = last_op
        self.pending_hints = pending_hints or []

    def __getattr__(self, column_name: str) -> Column:
        return self[column_name]

    def __getitem__(self, column_name: str) -> Column:
        column_name = f"{self.branch_id}.{column_name}"
        return Column(column_name)

    def __copy__(self):
        return self.copy()

    @property
    def sparkSession(self):
        return self.spark

    @property
    def write(self):
        return DataFrameWriter(self)

    @property
    def latest_cte_name(self) -> str:
        if len(self.expression.ctes) == 0:
            from_exp = self.expression.args['from']
            if from_exp.alias_or_name:
                return from_exp.alias_or_name
            table_alias = from_exp.find(exp.TableAlias)
            if not table_alias:
                raise RuntimeError(f"Could not find an alias name for this expression: {self.expression}")
            return table_alias.alias_or_name
        return self.expression.ctes[-1].alias
    
    @property
    def pending_join_hints(self):
        return [hint for hint in self.pending_hints if isinstance(hint, exp.JoinHint)]
    
    @property
    def pending_partition_hints(self):
        return [hint for hint in self.pending_hints if isinstance(hint, exp.Anonymous)]

    @property
    def columns(self) -> t.List[str]:
        return self.expression.named_selects

    def _replace_cte_names_with_hashes(self, expression: t.Union[exp.Select, exp.Create, exp.Insert]):
        def _replace_old_id_with_new(node):
            if isinstance(node, exp.Identifier) and node.alias_or_name == old_name_id.alias_or_name:
                node = node.replace(new_hashed_id)
            return node

        expression = expression.copy()
        ctes = expression.expression.ctes if isinstance(expression, (exp.Create, exp.Insert)) else expression.ctes
        for cte in ctes:
            old_name_id = cte.args['alias'].this
            new_hashed_id = exp.to_identifier(self._create_hash_from_expression(cte.this), quoted=old_name_id.args['quoted'])
            cte.set("alias", exp.TableAlias(this=new_hashed_id))
            expression = expression.transform(_replace_old_id_with_new)
        return expression

    def _create_cte_from_expression(self, expression: exp.Expression, branch_id: t.Optional[str] = None,
                                    sequence_id: t.Optional[str] = None, **kwargs) -> t.Tuple[exp.CTE, str]:
        name = self.spark._random_name
        expression_to_cte = expression.copy()
        expression_to_cte.set("with", None)
        cte = exp.Select().with_(name, as_=expression_to_cte, **kwargs).ctes[0]
        cte.set("branch_id", branch_id or self.branch_id)
        cte.set("sequence_id", sequence_id or self.sequence_id)
        return cte, name

    def _ensure_list_of_columns(self, cols: t.Union[str, t.Iterable[str], Column, t.Iterable[Column]]) -> t.List[Column]:
        columns = ensure_list(cols)
        columns = Column.ensure_cols(columns)
        return columns

    def _ensure_and_sanitize_cols(self, cols):
        cols = self._ensure_list_of_columns(cols)
        sanitize(self.spark, self.expression, cols)
        return cols

    def _ensure_and_sanitize_col(self, col):
        col = Column.ensure_col(col)
        sanitize(self.spark, self.expression, col)
        return col

    def _convert_leaf_to_cte(self, sequence_id: t.Optional[str] = None) -> DataFrame:
        df = self._resolve_pending_hints()
        sequence_id = sequence_id or df.sequence_id
        expression = df.expression.copy()
        cte_expression, cte_name = df._create_cte_from_expression(expression=expression, sequence_id=sequence_id)
        new_expression = exp.Select()
        new_expression = df._add_ctes_to_expression(new_expression, expression.ctes + [cte_expression])
        sel_columns = df._get_outer_select_columns(cte_expression)
        star_columns = [col for col in sel_columns if isinstance(col.expression.this, exp.Star)]
        if len(star_columns) > 0:
            sel_columns = star_columns[:1]
        new_expression = new_expression.from_(cte_name).select(*[x.alias_or_name for x in sel_columns])
        return df.copy(expression=new_expression, sequence_id=sequence_id)

    def _resolve_pending_hints(self) -> DataFrame:
        if not self.pending_join_hints and not self.pending_partition_hints:
            return self.copy()
        df = self.copy()
        expression = df.expression
        hint_expression = expression.args.get("hint") or exp.Hint(expressions=[])
        for hint in df.pending_partition_hints:
            hint_expression.args.get("expressions").append(hint)
            df.pending_hints.remove(hint)

        join_tables = get_tables_from_expression_with_join(expression)
        if join_tables:
            for hint in df.pending_join_hints:
                for sequence_id_expression in hint.expressions:
                    sequence_id_or_name = sequence_id_expression.alias_or_name
                    sequence_ids_to_match = [sequence_id_or_name]
                    if sequence_id_or_name in df.spark.name_to_sequence_id_mapping:
                        sequence_ids_to_match = df.spark.name_to_sequence_id_mapping[sequence_id_or_name]
                    matching_ctes = [cte for cte in reversed(expression.ctes) if cte.args["sequence_id"] in sequence_ids_to_match]
                    if matching_ctes:
                        for matching_cte in matching_ctes:
                            if matching_cte.alias_or_name in [join_table.alias_or_name for join_table in join_tables]:
                                sequence_id_expression.set("this", matching_cte.args['alias'].this)
                                df.pending_hints.remove(hint)
                                break
                hint_expression.args.get("expressions").append(hint)
        if hint_expression.expressions:
            expression.set("hint", hint_expression)
        return df

    def _hint(self, hint_name: str, args: t.List[Column]) -> DataFrame:
        hint_name = hint_name.upper()
        if hint_name in {
            "BROADCAST",
            "BROADCASTJOIN",
            "MAPJOIN",
            "MERGE",
            "SHUFFLEMERGE",
            "MERGEJOIN",
            "SHUFFLE_HASH",
            "SHUFFLE_REPLICATE_NL",
        }:
            hint_expression = exp.JoinHint(this=hint_name, expressions=[exp.to_table(parameter.alias_or_name) for parameter in args])
        else:
            hint_expression = exp.Anonymous(this=hint_name, expressions=[parameter.expression for parameter in args])
        new_df = self.copy()
        new_df.pending_hints.append(hint_expression)
        return new_df

    def _set_operation(self, clazz: t.Callable, other: DataFrame, distinct: bool):
        other_df = other._convert_leaf_to_cte()
        base_expression = self.expression.copy()
        base_expression = self._add_ctes_to_expression(base_expression, other_df.expression.ctes)
        all_ctes = base_expression.ctes
        other_df.expression.set("with", None)
        base_expression.set("with", None)
        operation = clazz(this=base_expression, distinct=distinct, expression=other_df.expression)
        operation.set("with", exp.With(expressions=all_ctes))
        return self.copy(expression=operation)._convert_leaf_to_cte()

    @classmethod
    def _add_ctes_to_expression(cls, expression: exp.Expression, ctes: t.List[exp.CTE]) -> exp.Expression:
        expression = expression.copy()
        with_expression = expression.args.get("with")
        if with_expression:
            existing_ctes = with_expression.args["expressions"]
            existsing_cte_names = [x.alias_or_name for x in existing_ctes]
            for cte in ctes:
                if cte.alias_or_name not in existsing_cte_names:
                    existing_ctes.append(cte)
        else:
            existing_ctes = ctes
        expression.set("with", exp.With(expressions=existing_ctes))
        return expression

    @classmethod
    def _get_outer_select_columns(cls, item: t.Union[exp.Expression, DataFrame]) -> t.List[Column]:
        expression = item.expression if isinstance(item, DataFrame) else item
        return [Column(x) for x in dict.fromkeys(expression.find(exp.Select).args.get("expressions", []))]

    @classmethod
    def _create_hash_from_expression(cls, expression: exp.Select):
        value = expression.sql(dialect="spark").encode("utf-8")
        return f"t{zlib.crc32(value)}"[:6]

    @classmethod
    def _select_expression(cls, expression: exp.Expression):
        if isinstance(expression, exp.Select):
            return expression
        elif isinstance(expression, (exp.Insert, exp.Create)):
            select_expression = expression.expression.copy()
            select_expression.set("with", expression.args.get("with"))
            return select_expression
        raise RuntimeError(f"Unexpected expression type: {type(expression)}")

    def sql(self, dialect="spark", optimize=True, **kwargs) -> str:
        df = self._resolve_pending_hints()
        expression = df.expression.copy()
        if optimize:
            optimized_select_expression = optimize_func(self._select_expression(expression))
            optimized_select_expression_without_ctes = optimized_select_expression.copy()
            optimized_select_expression_without_ctes.set("with", None)
            if isinstance(expression, (exp.Create, exp.Insert)):
                expression.set("expression", optimized_select_expression_without_ctes)
                expression.set("with", optimized_select_expression.args['with'])
            else:
                expression = optimized_select_expression
        expression = self._replace_cte_names_with_hashes(expression)
        return expression.sql(**{"dialect": dialect, "pretty": True, **kwargs})

    def cache(self) -> DataFrame:
        print("DataFrame Cache is not yet supported")
        return self

    def copy(self, **kwargs) -> DataFrame:
        kwargs = {**{k: copy(v) for k, v in vars(self).copy().items()}, **kwargs}
        return DataFrame(**kwargs)

    @operation(Operation.SELECT)
    def select(self, *cols, **kwargs) -> DataFrame:
        cols = self._ensure_and_sanitize_cols(cols)
        kwargs["append"] = kwargs.get("append", False)
        if self.expression.args.get("joins"):
            ambiguous_cols = [col for col in cols if not col.column_expression.table]
            if ambiguous_cols:
                join_table_identifiers = [x.this for x in get_tables_from_expression_with_join(self.expression)]
                cte_names_in_join = [x.this for x in join_table_identifiers]
                for ambiguous_col in ambiguous_cols:
                    ctes_with_column = [cte for cte in self.expression.ctes if cte.alias_or_name in cte_names_in_join and ambiguous_col.alias_or_name in cte.args['this'].named_selects]
                    # If the select column does not specify a table and there is a join
                    # then we assume they are referring to the left table
                    if len(ctes_with_column) > 1:
                        table_identifier = self.expression.args['from'].args['expressions'][0].this
                    else:
                        table_identifier = ctes_with_column[0].args['alias'].this
                    ambiguous_col.expression.set("table", table_identifier)
        expression = self.expression.select(*[x.expression for x in cols], **kwargs)
        qualify_columns(expression, sqlglot.schema)
        return self.copy(expression=expression, **kwargs)

    @operation(Operation.NO_OP)
    def alias(self, name: str, **kwargs) -> DataFrame:
        new_sequence_id = self.spark._random_sequence_id
        df = self.copy()
        for join_hint in df.pending_join_hints:
            for expression in join_hint.expressions:
                if expression.alias_or_name == self.sequence_id:
                    expression.set("this", Column.ensure_col(new_sequence_id).expression)
        df.spark._add_alias_to_mapping(name, new_sequence_id)
        return df._convert_leaf_to_cte(sequence_id=new_sequence_id)

    @operation(Operation.WHERE)
    def where(self, column: t.Union[Column, bool], **kwargs) -> DataFrame:
        column = self._ensure_and_sanitize_col(column)
        return self.copy(expression=self.expression.where(column.expression))

    filter = where

    @operation(Operation.GROUP_BY)
    def groupBy(self, *cols, **kwargs) -> GroupedData:
        cols = self._ensure_and_sanitize_cols(cols)
        return GroupedData(self, cols)

    @operation(Operation.SELECT)
    def agg(self, *exprs, **kwargs) -> DataFrame:
        cols = self._ensure_and_sanitize_cols(exprs)
        return self.groupBy().agg(*cols)

    @operation(Operation.FROM)
    def join(self, other_df: DataFrame, on: t.Union[str, t.List[str], Column, t.List[Column]], how: str = 'inner', **kwargs) -> DataFrame:
        other_df = other_df._convert_leaf_to_cte()
        pre_join_self_latest_cte_name = self.latest_cte_name
        columns = self._ensure_and_sanitize_cols(on)
        join_type = how.replace("_", " ")
        if isinstance(columns[0].expression, exp.Column):
            join_columns = [Column(x).set_table_name(pre_join_self_latest_cte_name) for x in columns]
            join_clause = functools.reduce(lambda x, y: x & y, [
                col.copy().set_table_name(pre_join_self_latest_cte_name) == col.copy().set_table_name(other_df.latest_cte_name)
                for col in columns
            ])
        else:
            if len(columns) > 1:
                columns = [functools.reduce(lambda x, y: x & y, columns)]
            join_clause = columns[0]
            join_columns = [
                Column(x).set_table_name(pre_join_self_latest_cte_name) if i % 2 == 0 else Column(x).set_table_name(other_df.latest_cte_name)
                for i, x in enumerate(join_clause.expression.find_all(exp.Column))
            ]
        self_columns = [column.set_table_name(pre_join_self_latest_cte_name, copy=True) for column in self._get_outer_select_columns(self)]
        other_columns = [column.set_table_name(other_df.latest_cte_name, copy=True) for column in self._get_outer_select_columns(other_df)]
        column_value_mapping = {column.alias_or_name if not isinstance(column.expression.this, exp.Star) else column.sql(): column for column in other_columns + self_columns + join_columns}
        all_columns = [column_value_mapping[name] for name in {x.alias_or_name: None for x in join_columns + self_columns + other_columns}]
        new_df = self.copy(expression=self.expression.join(other_df.latest_cte_name, on=join_clause.expression, join_type=join_type))
        new_df.expression = new_df._add_ctes_to_expression(new_df.expression, other_df.expression.ctes)
        new_df.pending_hints.extend(other_df.pending_hints)
        new_df = new_df.select.__wrapped__(new_df, *all_columns)
        return new_df

    @operation(Operation.ORDER_BY)
    def orderBy(self, *cols: t.Union[str, Column], ascending: t.Optional[t.Union[t.Any, t.List[t.Any]]] = None) -> DataFrame:
        """
        This implementation lets any ordered columns take priority over whatever is provided in `ascending`. Spark
        has irregular behavior and can result in runtime errors. Users shouldn't be mixing the two anyways so this
        is unlikely to come up.
        """
        cols = self._ensure_and_sanitize_cols(cols)
        pre_ordered_col_indexes = [x for x in [i if isinstance(col.expression, exp.Ordered) else None for i, col in enumerate(cols)] if x is not None]
        if ascending is None:
            ascending = [True] * len(cols)
        elif not isinstance(ascending, list):
            ascending = [ascending] * len(cols)
        ascending = [bool(x) for i, x in enumerate(ascending)]
        assert len(cols) == len(ascending), "The length of items in ascending must equal the number of columns provided"
        col_and_ascending = list(zip(cols, ascending))
        order_by_columns = [
            exp.Ordered(this=col.expression, desc=not asc)
            if i not in pre_ordered_col_indexes else cols[i].column_expression
            for i, (col, asc) in enumerate(col_and_ascending)
        ]
        return self.copy(expression=self.expression.order_by(*order_by_columns))

    sort = orderBy

    @operation(Operation.FROM)
    def union(self, other: DataFrame) -> DataFrame:
        return self._set_operation(exp.Union, other, False)

    unionAll = union

    @operation(Operation.FROM)
    def unionByName(self, other: DataFrame, allowMissingColumns: bool = False):
        l_columns = self.columns
        r_columns = other.columns
        if not allowMissingColumns:
            l_expressions = l_columns
            r_expressions = l_columns
        else:
            l_expressions = []
            r_expressions = []
            r_columns_unused = copy(r_columns)
            for l_column in l_columns:
                l_expressions.append(l_column)
                if l_column in r_columns:
                    r_expressions.append(l_column)
                    r_columns_unused.remove(l_column)
                else:
                    r_expressions.append(exp.alias_(exp.Null(), l_column))
            for r_column in r_columns_unused:
                l_expressions.append(exp.alias_(exp.Null(), r_column))
                r_expressions.append(r_column)
        r_df = other.copy()._convert_leaf_to_cte().select(*self._ensure_list_of_columns(r_expressions))
        l_df = self.copy()
        if allowMissingColumns:
            l_df = l_df._convert_leaf_to_cte().select(*self._ensure_list_of_columns(l_expressions))
        return l_df._set_operation(exp.Union, r_df, False)

    @operation(Operation.FROM)
    def intersect(self, other: DataFrame) -> DataFrame:
        return self._set_operation(exp.Intersect, other, True)

    @operation(Operation.FROM)
    def intersectAll(self, other: DataFrame) -> DataFrame:
        return self._set_operation(exp.Intersect, other, False)

    @operation(Operation.FROM)
    def exceptAll(self, other: DataFrame) -> DataFrame:
        return self._set_operation(exp.Except, other, False)

    @operation(Operation.SELECT)
    def distinct(self) -> DataFrame:
        expression = self.expression.copy()
        expression.set("distinct", exp.Distinct())
        return self.copy(expression=expression)

    @property
    def na(self) -> DataFrameNaFunctions:
        return DataFrameNaFunctions(self)

    @operation(Operation.FROM)
    def dropna(self, how: str = "any", thresh: t.Optional[int] = None,
               subset: t.Optional[t.Union[str, t.Tuple[str, ...], t.List[str]]] = None) -> DataFrame:
        if self.expression.ctes[-1].find(exp.Star) is not None:
            raise RuntimeError("Cannot use `dropna` when a * expression is used")
        minimum_non_null = thresh
        new_df = self.copy()
        all_columns = self._get_outer_select_columns(new_df.expression)
        if subset:
            null_check_columns = self._ensure_and_sanitize_cols(subset)
        else:
            null_check_columns = all_columns
        if thresh is None:
            minimum_num_nulls = 1 if how == "any" else len(null_check_columns)
        else:
            minimum_num_nulls = len(null_check_columns) - minimum_non_null + 1
        if minimum_num_nulls > len(null_check_columns):
            raise RuntimeError(f"The minimum num nulls for dropna must be less than or equal to the number of columns. "
                               f"Minimum num nulls: {minimum_num_nulls}, Num Columns: {len(null_check_columns)}")
        if_null_checks = [
            F.when(column.isNull(), F.lit(1)).otherwise(F.lit(0))
            for column in null_check_columns
        ]
        nulls_added_together = functools.reduce(lambda x, y: x + y, if_null_checks)
        num_nulls = nulls_added_together.alias("num_nulls")
        new_df = new_df.select(num_nulls, append=True)
        filtered_df = new_df.where(F.col("num_nulls") < F.lit(minimum_num_nulls))
        final_df = filtered_df.select(*all_columns)
        return final_df

    @operation(Operation.FROM)
    def fillna(self,
               value: t.Union[int, bool, float, str, t.Dict[str, t.Any]],
               subset: t.Optional[t.Union[str, t.Tuple[str, ...], t.List[str]]] = None) -> DataFrame:
        """
        Functionality Difference: If you provide a value to replace a null and that type conflicts
        with the type of the column then PySpark will just ignore your replacement.
        This will try to cast them to be the same in some cases. So they won't always match.
        Best to not mix types so make sure replacement is the same type as the column

        Possibility for improvement: Use `typeof` function to get the type of the column
        and check if it matches the type of the value provided. If not then make it null.
        """
        from sqlglot.dataframe.sql.functions import lit
        values = None
        columns = None
        new_df = self.copy()
        all_columns = self._get_outer_select_columns(new_df.expression)
        all_column_mapping = {
            column.alias_or_name: column
            for column in all_columns
        }
        if isinstance(value, dict):
            values = value.values()
            columns = self._ensure_and_sanitize_cols(list(value.keys()))
        if not columns:
            columns = self._ensure_and_sanitize_cols(subset) if subset else all_columns
        if not values:
            values = [value] * len(columns)
        values = [lit(value) for value in values]

        null_replacement_mapping = {
            column.alias_or_name: (
                F.when(column.isNull(), value)
                .otherwise(column)
                .alias(column.alias_or_name)
            )
            for column, value in zip(columns, values)
        }
        null_replacement_mapping = {**all_column_mapping, **null_replacement_mapping}
        null_replacement_columns = [
            null_replacement_mapping[column.alias_or_name]
            for column in all_columns
        ]
        new_df = new_df.select(*null_replacement_columns)
        return new_df

    @operation(Operation.FROM)
    def replace(self, to_replace: t.Union[bool, int, float, str, t.List, t.Dict],
                value: t.Optional[t.Union[bool, int, float, str, t.List]] = None,
                subset: t.Optional[t.Union[str, t.List[str]]] = None) -> DataFrame:
        from sqlglot.dataframe.sql.functions import lit
        old_values = None
        subset = ensure_list(subset)
        new_df = self.copy()
        all_columns = self._get_outer_select_columns(new_df.expression)
        all_column_mapping = {
            column.alias_or_name: column
            for column in all_columns
        }

        columns = self._ensure_and_sanitize_cols(subset) if subset else all_columns
        if isinstance(to_replace, dict):
            old_values = list(to_replace.keys())
            new_values = list(to_replace.values())
        elif not old_values and isinstance(to_replace, list):
            assert isinstance(value, list), "value must be a list since the replacements are a list"
            assert len(to_replace) == len(value), "the replacements and values must be the same length"
            old_values = to_replace
            new_values = value
        else:
            old_values = [to_replace] * len(columns)
            new_values = [value] * len(columns)
        old_values = [lit(value) for value in old_values]
        new_values = [lit(value) for value in new_values]

        replacement_mapping = {}
        for column in columns:
            expression = None
            for i, (old_value, new_value) in enumerate(zip(old_values, new_values)):
                if i == 0:
                    expression = F.when(column == old_value, new_value)
                else:
                    expression = expression.when(column == old_value, new_value)
            replacement_mapping[column.alias_or_name] = expression.otherwise(column).alias(column.expression.alias_or_name)

        replacement_mapping = {**all_column_mapping, **replacement_mapping}
        replacement_columns = [
            replacement_mapping[column.alias_or_name]
            for column in all_columns
        ]
        new_df = new_df.select(*replacement_columns)
        return new_df

    @operation(Operation.SELECT)
    def withColumn(self, colName: str, col: Column) -> DataFrame:
        col = self._ensure_and_sanitize_col(col)
        existing_col_names = self.expression.named_selects
        existing_col_index = existing_col_names.index(colName) if colName in existing_col_names else None
        if existing_col_index:
            expression = self.expression.copy()
            expression.expressions[existing_col_index] = col.expression
            return self.copy(expression=expression)
        return self.copy().select(col.alias(colName), append=True)

    @operation(Operation.SELECT)
    def withColumnRenamed(self, existing: str, new: str):
        expression = self.expression.copy()
        existing_columns = [expression for expression in expression.expressions if expression.alias_or_name == existing]
        if not existing_columns:
            raise ValueError("Tried to rename a column that doesn't exist")
        for existing_column in existing_columns:
            if isinstance(existing_column, exp.Column):
                existing_column.replace(exp.alias_(existing_column.copy(), new))
            else:
                existing_column.set("alias", exp.to_identifier(new))
        return self.copy(expression=expression)

    @operation(Operation.SELECT)
    def drop(self, *cols: t.Union[str, Column]) -> DataFrame:
        all_columns = self._get_outer_select_columns(self.expression)
        drop_cols = self._ensure_and_sanitize_cols(cols)
        new_columns = [col for col in all_columns if
                       col.alias_or_name not in [drop_column.alias_or_name for drop_column in drop_cols]]
        return self.copy().select(*new_columns, append=False)

    @operation(Operation.LIMIT)
    def limit(self, num: int) -> DataFrame:
        return self.copy(expression=self.expression.limit(num))

    @operation(Operation.NO_OP)
    def hint(self, name: str, *parameters: t.Optional[t.Union[int, str]]) -> DataFrame:
        parameters = ensure_list(parameters)
        parameters = self._ensure_list_of_columns(parameters) if parameters else Column.ensure_cols([self.sequence_id])
        return self._hint(name, parameters)

    @operation(Operation.NO_OP)
    def repartition(self, numPartitions: t.Union[int, str], *cols: t.Union[int, str]) -> DataFrame:
        num_partitions = Column.ensure_cols([numPartitions])
        cols = self._ensure_and_sanitize_cols(cols)
        args = num_partitions + cols
        return self._hint("repartition", args)

    @operation(Operation.NO_OP)
    def coalesce(self, numPartitions: int) -> DataFrame:
        num_partitions = Column.ensure_cols([numPartitions])
        return self._hint("coalesce", num_partitions)


class DataFrameNaFunctions:
    def __init__(self, df: DataFrame):
        self.df = df

    def drop(self, how: str = "any", thresh: t.Optional[int] = None,
             subset: t.Optional[t.Union[str, t.Tuple[str, ...], t.List[str]]] = None) -> DataFrame:
        return self.df.dropna(how=how, thresh=thresh, subset=subset)

    def fill(self,
             value: t.Union[int, bool, float, str, t.Dict[str, t.Any]],
             subset: t.Optional[t.Union[str, t.Tuple[str, ...], t.List[str]]] = None) -> DataFrame:
        return self.df.fillna(value=value, subset=subset)

    def replace(self, to_replace: t.Union[bool, int, float, str, t.List, t.Dict],
                value: t.Optional[t.Union[bool, int, float, str, t.List]] = None,
                subset: t.Optional[t.Union[str, t.List[str]]] = None) -> DataFrame:
        return self.df.replace(to_replace=to_replace, value=value, subset=subset)
