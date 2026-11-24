# 供应商产能回报采集与订单承诺调整

合成供应商门户产能、共享工序和客户订单演练资料，数量均为周产件数。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `capacity.csv` contains 供应商门户采集到的产品或工序周产能。 Columns are `record`, `supplier`, `resource`, `week`, `quantity`, `revision`.
- `orders.csv` contains 客户需求、交期和承诺状态。 Columns are `order_id`, `product`, `week`, `quantity`, `due`, `state`.
- `routing.csv` contains 产品经过共享设备的工艺路由。 Columns are `product`, `resource`, `units_per_lot`, `valid_from`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
