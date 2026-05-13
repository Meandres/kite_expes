SELECT
    s_name,
    s_address
FROM
    '{source}/supplier.parquet',
    '{source}/nation.parquet'
WHERE
    s_suppkey IN (
        SELECT
            ps_suppkey
        FROM
            '{source}/partsupp.parquet'
        WHERE
            ps_partkey IN (
                SELECT
                    p_partkey
                FROM
                    '{source}/part.parquet'
                WHERE
                    p_name LIKE 'forest%')
                AND ps_availqty > (
                    SELECT
                        0.5 * sum(l_quantity)
                    FROM
                        '{source}/lineitem.parquet'
                    WHERE
                        l_partkey = ps_partkey
                        AND l_suppkey = ps_suppkey
                        AND l_shipdate >= CAST('1994-01-01' AS date)
                        AND l_shipdate < CAST('1995-01-01' AS date)))
            AND s_nationkey = n_nationkey
            AND n_name = 'CANADA'
        ORDER BY
            s_name;
