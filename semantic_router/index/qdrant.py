from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from pydantic import Field

from semantic_router.index.base import BaseIndex, IndexConfig
from semantic_router.schema import ConfigParameter, Metric, SparseEmbedding, Utterance
from semantic_router.utils.logger import logger

DEFAULT_COLLECTION_NAME = "semantic-router-index"
DEFAULT_UPLOAD_BATCH_SIZE = 100
SCROLL_SIZE = 1000
SR_UTTERANCE_PAYLOAD_KEY = "sr_utterance"
SR_ROUTE_PAYLOAD_KEY = "sr_route"


class QdrantIndex(BaseIndex):
    "The name of the collection to use"

    index_name: str = Field(
        default=DEFAULT_COLLECTION_NAME,
        description="Name of the Qdrant collection."
        f"Default: '{DEFAULT_COLLECTION_NAME}'",
    )
    location: Optional[str] = Field(
        default=":memory:",
        description="If ':memory:' - use an in-memory Qdrant instance."
        "Used as 'url' value otherwise",
    )
    url: Optional[str] = Field(
        default=None,
        description="Qualified URL of the Qdrant instance."
        "Optional[scheme], host, Optional[port], Optional[prefix]",
    )
    port: Optional[int] = Field(
        default=6333,
        description="Port of the REST API interface.",
    )
    grpc_port: int = Field(
        default=6334,
        description="Port of the gRPC interface.",
    )
    prefer_grpc: Optional[bool] = Field(
        default=None,
        description="Whether to use gPRC interface whenever possible in methods",
    )
    https: Optional[bool] = Field(
        default=None,
        description="Whether to use HTTPS(SSL) protocol.",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="API key for authentication in Qdrant Cloud.",
    )
    prefix: Optional[str] = Field(
        default=None,
        description="Prefix to the REST URL path. Example: `http://localhost:6333/some/prefix/{qdrant-endpoint}`.",
    )
    timeout: Optional[int] = Field(
        default=None,
        description="Timeout for REST and gRPC API requests.",
    )
    host: Optional[str] = Field(
        default=None,
        description="Host name of Qdrant service."
        "If url and host are None, set to 'localhost'.",
    )
    path: Optional[str] = Field(
        default=None,
        description="Persistence path for Qdrant local",
    )
    grpc_options: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Options to be passed to the low-level GRPC client, if used.",
    )
    dimensions: Union[int, None] = Field(
        default=None,
        description="Embedding dimensions."
        "Defaults to the embedding length of the configured encoder.",
    )
    metric: Metric = Field(
        default=Metric.COSINE,
        description="Distance metric to use for similarity search.",
    )
    config: Optional[Dict[str, Any]] = Field(
        default={},
        description="Collection options passed to `QdrantClient#create_collection`.",
    )
    client: Any = Field(default=None, exclude=True)
    aclient: Any = Field(default=None, exclude=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.type = "qdrant"
        self.client, self.aclient = self._initialize_clients()

    def _initialize_clients(self):
        """Initialize the clients for the Qdrant index.

        :return: A tuple of the sync and async clients.
        :rtype: Tuple[QdrantClient, Optional[AsyncQdrantClient]]
        """
        try:
            from qdrant_client import AsyncQdrantClient, QdrantClient

            sync_client = QdrantClient(
                location=self.location,
                url=self.url,
                port=self.port,
                grpc_port=self.grpc_port,
                prefer_grpc=self.prefer_grpc,
                https=self.https,
                api_key=self.api_key,
                prefix=self.prefix,
                timeout=self.timeout,
                host=self.host,
                path=self.path,
                grpc_options=self.grpc_options,
            )

            async_client: Optional[AsyncQdrantClient] = None

            if all([self.location != ":memory:", self.path is None]):
                # Local Qdrant cannot interoperate with sync and async clients
                # We fallback to sync operations in this case
                async_client = AsyncQdrantClient(
                    location=self.location,
                    url=self.url,
                    port=self.port,
                    grpc_port=self.grpc_port,
                    prefer_grpc=self.prefer_grpc,
                    https=self.https,
                    api_key=self.api_key,
                    prefix=self.prefix,
                    timeout=self.timeout,
                    host=self.host,
                    path=self.path,
                    grpc_options=self.grpc_options,
                )

            return sync_client, async_client
        except ImportError as e:
            raise ImportError(
                "Please install 'qdrant-client' to use QdrantIndex."
                "You can install it with: "
                "`pip install 'semantic-router[qdrant]'`"
            ) from e

    def _init_collection(self) -> None:
        """Initialize the collection for the Qdrant index.

        :return: None
        :rtype: None
        """
        from qdrant_client import QdrantClient, models

        self.client: QdrantClient
        if not self.client.collection_exists(self.index_name):
            if not self.dimensions:
                raise ValueError(
                    "Cannot create a collection without specifying the dimensions."
                )

            self.client.create_collection(
                collection_name=self.index_name,
                vectors_config=models.VectorParams(
                    size=self.dimensions, distance=self.convert_metric(self.metric)
                ),
                **self.config,
            )

    def _remove_and_sync(self, routes_to_delete: dict):
        """Remove and sync the index.

        :param routes_to_delete: The routes to delete.
        :type routes_to_delete: dict
        """
        logger.error("Sync remove is not implemented for QdrantIndex.")

    def add(
        self,
        embeddings: List[List[float]],
        routes: List[str],
        utterances: List[str],
        function_schemas: Optional[List[Dict[str, Any]]] = None,
        metadata_list: List[Dict[str, Any]] = [],
        batch_size: int = DEFAULT_UPLOAD_BATCH_SIZE,
        **kwargs,
    ):
        """Add records to the index.

        :param embeddings: The embeddings to add.
        :type embeddings: List[List[float]]
        :param routes: The routes to add.
        :type routes: List[str]
        :param utterances: The utterances to add.
        :type utterances: List[str]
        :param function_schemas: The function schemas to add.
        :type function_schemas: Optional[List[Dict[str, Any]]]
        :param metadata_list: The metadata to add.
        :type metadata_list: List[Dict[str, Any]]
        :param batch_size: The batch size to use for the upload.
        :type batch_size: int
        """
        self.dimensions = self.dimensions or len(embeddings[0])
        self._init_collection()

        payloads = [
            {SR_ROUTE_PAYLOAD_KEY: route, SR_UTTERANCE_PAYLOAD_KEY: utterance}
            for route, utterance in zip(routes, utterances)
        ]

        # UUIDs are autogenerated by qdrant-client if not provided explicitly
        self.client.upload_collection(
            self.index_name,
            vectors=embeddings,
            payload=payloads,
            batch_size=batch_size,
        )

    def get_utterances(self, include_metadata: bool = False) -> List[Utterance]:
        """Gets a list of route and utterance objects currently stored in the index.

        :param include_metadata: Whether to include function schemas and metadata in
        the returned Utterance objects - QdrantIndex does not currently support this
        parameter so it is ignored. If required for your use-case please reach out to
        semantic-router maintainers on GitHub via an issue or PR.
        :type include_metadata: bool
        :return: A list of Utterance objects.
        :rtype: List[Utterance]
        """
        # Check if collection exists first
        if not self.client.collection_exists(self.index_name):
            return []

        from qdrant_client import grpc

        results = []
        next_offset = None
        stop_scrolling = False
        try:
            while not stop_scrolling:
                records, next_offset = self.client.scroll(
                    self.index_name,
                    limit=SCROLL_SIZE,
                    offset=next_offset,
                    with_payload=True,
                )
                stop_scrolling = next_offset is None or (
                    isinstance(next_offset, grpc.PointId)
                    and next_offset.num == 0
                    and next_offset.uuid == ""
                )

                results.extend(records)

            utterances: List[Utterance] = [
                Utterance(
                    route=x.payload[SR_ROUTE_PAYLOAD_KEY],
                    utterance=x.payload[SR_UTTERANCE_PAYLOAD_KEY],
                    function_schemas=None,
                    metadata={},
                )
                for x in results
            ]
        except ValueError as e:
            logger.warning(f"Index likely empty, error: {e}")
            return []
        return utterances

    def delete(self, route_name: str):
        """Delete records from the index.

        :param route_name: The name of the route to delete.
        :type route_name: str
        """
        from qdrant_client import models

        self.client.delete(
            self.index_name,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key=SR_ROUTE_PAYLOAD_KEY,
                        match=models.MatchText(text=route_name),
                    )
                ]
            ),
        )

    def describe(self) -> IndexConfig:
        """Describe the index.

        :return: The index configuration.
        :rtype: IndexConfig
        """
        collection_info = self.client.get_collection(self.index_name)

        return IndexConfig(
            type=self.type,
            dimensions=collection_info.config.params.vectors.size,
            vectors=collection_info.points_count,
        )

    def is_ready(self) -> bool:
        """Checks if the index is ready to be used.

        :return: True if the index is ready, False otherwise.
        :rtype: bool
        """
        return self.client.collection_exists(self.index_name)

    def query(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        route_filter: Optional[List[str]] = None,
        sparse_vector: dict[int, float] | SparseEmbedding | None = None,
    ) -> Tuple[np.ndarray, List[str]]:
        """Query the index.

        :param vector: The vector to query.
        :type vector: np.ndarray
        :param top_k: The number of results to return.
        :type top_k: int
        :param route_filter: The route filter to apply.
        :type route_filter: Optional[List[str]]
        :param sparse_vector: The sparse vector to query.
        :type sparse_vector: dict[int, float] | SparseEmbedding | None
        :return: A tuple of the scores and route names.
        :rtype: Tuple[np.ndarray, List[str]]
        """
        from qdrant_client import QdrantClient, models

        self.client: QdrantClient
        filter = None
        if route_filter is not None:
            filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key=SR_ROUTE_PAYLOAD_KEY,
                        match=models.MatchAny(any=route_filter),
                    )
                ]
            )

        results = self.client.query_points(
            self.index_name,
            query=vector,
            limit=top_k,
            with_payload=True,
            query_filter=filter,
        )
        scores = [result.score for result in results.points]
        route_names = [
            result.payload[SR_ROUTE_PAYLOAD_KEY] for result in results.points
        ]
        return np.array(scores), route_names

    async def aquery(
        self,
        vector: np.ndarray,
        top_k: int = 5,
        route_filter: Optional[List[str]] = None,
        sparse_vector: dict[int, float] | SparseEmbedding | None = None,
    ) -> Tuple[np.ndarray, List[str]]:
        """Asynchronously query the index.

        :param vector: The vector to query.
        :type vector: np.ndarray
        :param top_k: The number of results to return.
        :type top_k: int
        :param route_filter: The route filter to apply.
        :type route_filter: Optional[List[str]]
        :param sparse_vector: The sparse vector to query.
        :type sparse_vector: dict[int, float] | SparseEmbedding | None
        :return: A tuple of the scores and route names.
        :rtype: Tuple[np.ndarray, List[str]]
        """
        from qdrant_client import AsyncQdrantClient, models

        self.aclient: Optional[AsyncQdrantClient]
        if self.aclient is None:
            logger.warning("Cannot use async query with an in-memory Qdrant instance")
            return self.query(vector, top_k, route_filter)

        filter = None
        if route_filter is not None:
            filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key=SR_ROUTE_PAYLOAD_KEY,
                        match=models.MatchAny(any=route_filter),
                    )
                ]
            )

        results = await self.aclient.query_points(
            self.index_name,
            query=vector,
            limit=top_k,
            with_payload=True,
            query_filter=filter,
        )
        scores = [result.score for result in results.points]
        route_names = [
            result.payload[SR_ROUTE_PAYLOAD_KEY] for result in results.points
        ]
        return np.array(scores), route_names

    def aget_routes(self):
        """Asynchronously get all routes from the index.

        :return: A list of routes.
        :rtype: List[str]
        """
        logger.error("Sync remove is not implemented for QdrantIndex.")

    def delete_index(self):
        """Delete the index.

        :return: None
        :rtype: None
        """
        self.client.delete_collection(self.index_name)

    def convert_metric(self, metric: Metric):
        """Convert the metric to a Qdrant distance metric.

        :param metric: The metric to convert.
        :type metric: Metric
        :return: The converted metric.
        :rtype: Distance
        """
        from qdrant_client.models import Distance

        mapping = {
            Metric.COSINE: Distance.COSINE,
            Metric.EUCLIDEAN: Distance.EUCLID,
            Metric.DOTPRODUCT: Distance.DOT,
            Metric.MANHATTAN: Distance.MANHATTAN,
        }

        if metric not in mapping:
            raise ValueError(f"Unsupported Qdrant similarity metric: {metric}")

        return mapping[metric]

    def _write_config(self, config: ConfigParameter):
        """Write the config to the index.

        :param config: The config to write to the index.
        :type config: ConfigParameter
        """
        logger.warning("No config is written for QdrantIndex.")

    def __len__(self):
        """Returns the total number of vectors in the index. If the index is not initialized
        returns 0.

        :return: The total number of vectors.
        :rtype: int
        """
        try:
            return self.client.get_collection(self.index_name).points_count
        except ValueError as e:
            logger.warning(f"No collection found, {e}")
            return 0
