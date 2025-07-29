# Planet Download

## Quickstart Example

First, create a `.env` file in your project root with your Planet API key:

```env
PL_API_KEY=your_planet_api_key_here
```

Then, install the required dependencies:

```bash
pip install python-dotenv geopandas pandas
```

Now you can use the library as follows:

```python
from planet_download.client import BasemapsClient

client = BasemapsClient()  # API key loaded automatically from .env
mosaics = list(client.list_mosaics())
print(mosaics[0].name)
```

See the `examples/examples_api.ipynb` notebook for more advanced usage.


