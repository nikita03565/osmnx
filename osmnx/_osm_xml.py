"""Read/write .osm formatted XML files."""

from __future__ import annotations

import bz2
import xml.sax
from pathlib import Path
from typing import Any
from typing import TextIO
from warnings import warn
from xml.etree import ElementTree as ET

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from . import settings
from . import utils
from . import utils_graph
from ._version import __version__


class _OSMContentHandler(xml.sax.handler.ContentHandler):
    """
    SAX content handler for OSM XML.

    Used to build an Overpass-like response JSON object in self.object. For
    format notes, see https://wiki.openstreetmap.org/wiki/OSM_XML and
    https://overpass-api.de
    """

    def __init__(self) -> None:
        self._element: dict[str, Any] | None = None
        self.object: dict[str, Any] = {"elements": []}

    def startElement(self, name: str, attrs: xml.sax.xmlreader.AttributesImpl) -> None:
        if name == "osm":
            self.object.update({k: v for k, v in attrs.items() if k in {"version", "generator"}})

        elif name in {"node", "way"}:
            self._element = dict(type=name, tags={}, nodes=[], **attrs)
            self._element.update({k: float(v) for k, v in attrs.items() if k in {"lat", "lon"}})
            self._element.update(
                {k: int(v) for k, v in attrs.items() if k in {"id", "uid", "version", "changeset"}}
            )

        elif name == "relation":
            self._element = dict(type=name, tags={}, members=[], **attrs)
            self._element.update(
                {k: int(v) for k, v in attrs.items() if k in {"id", "uid", "version", "changeset"}}
            )

        elif name == "tag":
            self._element["tags"].update({attrs["k"]: attrs["v"]})  # type: ignore[index]

        elif name == "nd":
            self._element["nodes"].append(int(attrs["ref"]))  # type: ignore[index]

        elif name == "member":
            self._element["members"].append(  # type: ignore[index]
                {k: (int(v) if k == "ref" else v) for k, v in attrs.items()}
            )

    def endElement(self, name: str) -> None:
        if name in {"node", "way", "relation"}:
            self.object["elements"].append(self._element)


def _overpass_json_from_file(filepath: str | Path, encoding: str) -> dict[str, Any]:
    """
    Read OSM XML from file and return Overpass-like JSON.

    Parameters
    ----------
    filepath : string or pathlib.Path
        path to file containing OSM XML data
    encoding : string
        the XML file's character encoding

    Returns
    -------
    response_json : dict
        a parsed JSON response from the Overpass API
    """

    # open the XML file, handling bz2 or regular XML
    def _opener(filepath: Path, encoding: str) -> TextIO:
        if filepath.suffix == ".bz2":
            return bz2.open(filepath, mode="rt", encoding=encoding)

        # otherwise just open it if it's not bz2
        return filepath.open(encoding=encoding)

    # warn if this XML file was generated by OSMnx itself
    with _opener(Path(filepath), encoding) as f:
        root_attrs = ET.parse(f).getroot().attrib
        if "generator" in root_attrs and "OSMnx" in root_attrs["generator"]:
            msg = (
                "The XML file you are loading appears to have been generated "
                "by OSMnx: this use case is not supported and may not behave "
                "as expected. To save/load graphs to/from disk for later use "
                "in OSMnx, use the `io.save_graphml` and `io.load_graphml` "
                "functions instead. Refer to the documentation for details."
            )
            warn(msg, stacklevel=2)

    # parse the XML to Overpass-like JSON
    with _opener(Path(filepath), encoding) as f:
        handler = _OSMContentHandler()
        xml.sax.parse(f, handler)
        return handler.object


def _save_graph_xml(
    data: nx.MultiDiGraph | tuple[gpd.GeoDataFrame, gpd.GeoDataFrame],
    filepath: str | Path | None,
    node_tags: list[str],
    node_attrs: list[str],
    edge_tags: list[str],
    edge_attrs: list[str],
    oneway: bool,
    merge_edges: bool,
    edge_tag_aggs: list[tuple[str, str]] | None,
    api_version: str,
    precision: int,
) -> None:
    """
    Save graph to disk as an OSM-formatted UTF-8 encoded XML .osm file.

    Parameters
    ----------
    data : networkx.MultiDiGraph or tuple of GeoDataFrames
        either a MultiDiGraph or (gdf_nodes, gdf_edges) tuple
    filepath : string or pathlib.Path
        path to the .osm file including extension. if None, use default data
        folder + graph.osm
    node_tags : list
        osm node tags to include in output OSM XML
    node_attrs: list
        osm node attributes to include in output OSM XML
    edge_tags : list
        osm way tags to include in output OSM XML
    edge_attrs : list
        osm way attributes to include in output OSM XML
    oneway : bool
        the default oneway value used to fill this tag where missing
    merge_edges : bool
        if True merges graph edges such that each OSM way has one entry
        and one entry only in the OSM XML. Otherwise, every OSM way
        will have a separate entry for each node pair it contains.
    edge_tag_aggs : list of length-2 string tuples
        useful only if merge_edges is True, this argument allows the user
        to specify edge attributes to aggregate such that the merged
        OSM way entry tags accurately represent the sum total of
        their component edge attributes. For example, if the user
        wants the OSM way to have a "length" attribute, the user must
        specify `edge_tag_aggs=[('length', 'sum')]` in order to tell
        this method to aggregate the lengths of the individual
        component edges. Otherwise, the length attribute will simply
        reflect the length of the first edge associated with the way.
    api_version : string
        OpenStreetMap API version to write to the XML file header
    precision : int
        number of decimal places to round latitude and longitude values

    Returns
    -------
    None
    """
    # default filepath if none was provided
    filepath = Path(settings.data_folder) / "graph.osm" if filepath is None else Path(filepath)

    # if save folder does not already exist, create it
    filepath.parent.mkdir(parents=True, exist_ok=True)

    if not settings.all_oneway:
        msg = (
            "For the `save_graph_xml` function to behave properly, the graph "
            "must have been created with `ox.settings.all_oneway=True`."
        )
        warn(msg, stacklevel=2)

    if isinstance(data, nx.MultiDiGraph):
        gdf_nodes, gdf_edges = utils_graph.graph_to_gdfs(
            data, node_geometry=False, fill_edge_geometry=False
        )
    elif isinstance(data, tuple):
        gdf_nodes, gdf_edges = data
    else:
        msg = "`data` must be a MultiDiGraph or a tuple of node/edge GeoDataFrames."
        raise TypeError(msg)

    # rename columns per osm specification
    gdf_nodes = gdf_nodes.rename(columns={"x": "lon", "y": "lat"})
    gdf_nodes["lon"] = gdf_nodes["lon"].round(precision)
    gdf_nodes["lat"] = gdf_nodes["lat"].round(precision)
    gdf_nodes = gdf_nodes.reset_index().rename(columns={"osmid": "id"})
    if "id" in gdf_edges.columns:
        gdf_edges = gdf_edges[[col for col in gdf_edges if col != "id"]]
    if "uniqueid" in gdf_edges.columns:
        gdf_edges = gdf_edges.rename(columns={"uniqueid": "id"})
    else:
        gdf_edges = gdf_edges.reset_index().reset_index().rename(columns={"index": "id"})

    # add default values for required attributes
    for table in (gdf_nodes, gdf_edges):
        table["uid"] = "1"
        table["user"] = "OSMnx"
        table["version"] = "1"
        table["changeset"] = "1"
        table["timestamp"] = utils.ts(template="{:%Y-%m-%dT%H:%M:%SZ}")

    # misc. string replacements to meet OSM XML spec
    if "oneway" in gdf_edges.columns:
        # fill blank oneway tags with default (False)
        gdf_edges.loc[pd.isna(gdf_edges["oneway"]), "oneway"] = oneway
        gdf_edges.loc[:, "oneway"] = gdf_edges["oneway"].astype(str)
        gdf_edges.loc[:, "oneway"] = (
            gdf_edges["oneway"].str.replace("False", "no").replace("True", "yes")
        )

    # initialize XML tree with an OSM root element then append nodes/edges
    root = ET.Element("osm", attrib={"version": api_version, "generator": f"OSMnx {__version__}"})
    root = _append_nodes_xml_tree(root, gdf_nodes, node_attrs, node_tags)
    root = _append_edges_xml_tree(
        root, gdf_edges, edge_attrs, edge_tags, edge_tag_aggs, merge_edges
    )

    # write to disk
    ET.ElementTree(root).write(filepath, encoding="utf-8", xml_declaration=True)
    utils.log(f"Saved graph as .osm file at {filepath!r}")


def _append_nodes_xml_tree(
    root: ET.Element, gdf_nodes: gpd.GeoDataFrame, node_attrs: list[str], node_tags: list[str]
) -> ET.Element:
    """
    Append nodes to an XML tree.

    Parameters
    ----------
    root : ElementTree.Element
        xml tree
    gdf_nodes : geopandas.GeoDataFrame
        GeoDataFrame of graph nodes
    node_attrs : list
        osm way attributes to include in output OSM XML
    node_tags : list
        osm way tags to include in output OSM XML

    Returns
    -------
    root : ElementTree.Element
        xml tree with nodes appended
    """
    for _, row in gdf_nodes.iterrows():
        row_str = row.dropna().astype(str)
        node = ET.SubElement(root, "node", attrib=row_str[node_attrs].to_dict())

        for tag in node_tags:
            if tag in row_str:
                ET.SubElement(node, "tag", attrib={"k": tag, "v": row_str[tag]})
    return root


def _create_way_for_each_edge(
    root: ET.Element, gdf_edges: gpd.GeoDataFrame, edge_attrs: list[str], edge_tags: list[str]
) -> None:
    """
    Append a new way to an empty XML tree graph for each edge in way.

    This will generate separate OSM ways for each network edge, even if the
    edges are all part of the same original OSM way. As such, each way will be
    composed of two nodes, and there will be many ways with the same OSM ID.
    This does not conform to the OSM XML schema standard, but the data will
    still comprise a valid network and will be readable by most OSM tools.

    Parameters
    ----------
    root : ElementTree.Element
        an empty XML tree
    gdf_edges : geopandas.GeoDataFrame
        GeoDataFrame of graph edges
    edge_attrs : list
        osm way attributes to include in output OSM XML
    edge_tags : list
        osm way tags to include in output OSM XML

    Returns
    -------
    None
    """
    for _, row in gdf_edges.iterrows():
        row_str = row.dropna().astype(str)
        edge = ET.SubElement(root, "way", attrib=row_str[edge_attrs].to_dict())
        ET.SubElement(edge, "nd", attrib={"ref": row_str["u"]})
        ET.SubElement(edge, "nd", attrib={"ref": row_str["v"]})
        for tag in edge_tags:
            if tag in row_str:
                ET.SubElement(edge, "tag", attrib={"k": tag, "v": row_str[tag]})


def _append_merged_edge_attrs(
    xml_edge: ET.Element,
    sample_edge: dict[str, Any],
    all_edges_df: pd.DataFrame,
    edge_tags: list[str],
    edge_tag_aggs: list[tuple[str, str]] | None,
) -> None:
    """
    Extract edge attributes and append to XML edge.

    Parameters
    ----------
    xml_edge : ElementTree.Element
        XML representation of an output graph edge
    sample_edge: dict
        dict of sample row from the the dataframe of way edges
    all_edges_df: pandas.DataFrame
        a dataframe with one row for each edge in an OSM way
    edge_tags : list
        osm way tags to include in output OSM XML
    edge_tag_aggs : list of length-2 string tuples
        useful only if merge_edges is True, this argument allows the user to
        specify edge attributes to aggregate such that the merged OSM way
        entry tags accurately represent the sum total of their component edge
        attributes. For example if the user wants the OSM way to have a length
        attribute, the user must specify `edge_tag_aggs=[('length', 'sum')]`
        to tell this method to aggregate the lengths of the individual
        component edges. Otherwise, the length attribute will simply reflect
        the length of the first edge associated with the way.

    Returns
    -------
    None
    """
    if edge_tag_aggs is None:
        for tag in edge_tags:
            if tag in sample_edge:
                ET.SubElement(xml_edge, "tag", attrib={"k": tag, "v": sample_edge[tag]})
    else:
        for tag in edge_tags:
            if (tag in sample_edge) and (tag not in (t for t, agg in edge_tag_aggs)):
                ET.SubElement(xml_edge, "tag", attrib={"k": tag, "v": sample_edge[tag]})

        for tag, agg in edge_tag_aggs:
            if tag in all_edges_df.columns:
                ET.SubElement(
                    xml_edge,
                    "tag",
                    attrib={
                        "k": tag,
                        "v": str(all_edges_df[tag].aggregate(agg)),
                    },
                )


def _append_nodes_as_edge_attrs(
    xml_edge: ET.Element, sample_edge: dict[str, Any], all_edges_df: pd.DataFrame
) -> None:
    """
    Extract list of ordered nodes and append as attributes of XML edge.

    Parameters
    ----------
    xml_edge : ElementTree.Element
        XML representation of an output graph edge
    sample_edge: dict
        sample row from the the dataframe of way edges
    all_edges_df: pandas.DataFrame
        a dataframe with one row for each edge in an OSM way

    Returns
    -------
    None
    """
    if len(all_edges_df) == 1:
        ET.SubElement(xml_edge, "nd", attrib={"ref": sample_edge["u"]})
        ET.SubElement(xml_edge, "nd", attrib={"ref": sample_edge["v"]})
    else:
        # topological sort
        all_edges_df = all_edges_df.reset_index()
        try:
            ordered_nodes = _get_unique_nodes_ordered_from_way(all_edges_df)
        except nx.NetworkXUnfeasible:
            first_node = all_edges_df.iloc[0]["u"]
            ordered_nodes = _get_unique_nodes_ordered_from_way(all_edges_df.iloc[1:])
            ordered_nodes = [first_node] + ordered_nodes
        for node in ordered_nodes:
            ET.SubElement(xml_edge, "nd", attrib={"ref": str(node)})


def _append_edges_xml_tree(
    root: ET.Element,
    gdf_edges: gpd.GeoDataFrame,
    edge_attrs: list[str],
    edge_tags: list[str],
    edge_tag_aggs: list[tuple[str, str]] | None,
    merge_edges: bool,
) -> ET.Element:
    """
    Append edges to an XML tree.

    Parameters
    ----------
    root : ElementTree.Element
        xml tree
    gdf_edges : geopandas.GeoDataFrame
        GeoDataFrame of graph edges
    edge_attrs : list
        osm way attributes to include in output OSM XML
    edge_tags : list
        osm way tags to include in output OSM XML
    edge_tag_aggs : list of length-2 string tuples
        useful only if merge_edges is True, this argument allows the user
        to specify edge attributes to aggregate such that the merged
        OSM way entry tags accurately represent the sum total of
        their component edge attributes. For example, if the user
        wants the OSM way to have a "length" attribute, the user must
        specify `edge_tag_aggs=[('length', 'sum')]` in order to tell
        this method to aggregate the lengths of the individual
        component edges. Otherwise, the length attribute will simply
        reflect the length of the first edge associated with the way.
    merge_edges : bool
        if True merges graph edges such that each OSM way has one entry
        and one entry only in the OSM XML. Otherwise, every OSM way
        will have a separate entry for each node pair it contains.

    Returns
    -------
    root : ElementTree.Element
        XML tree with edges appended
    """
    gdf_edges = gdf_edges.reset_index()
    if merge_edges:
        for _, all_way_edges in gdf_edges.groupby("id"):
            first = all_way_edges.iloc[0].dropna().astype(str)
            edge = ET.SubElement(root, "way", attrib=first[edge_attrs].dropna().to_dict())
            _append_nodes_as_edge_attrs(
                xml_edge=edge, sample_edge=first.to_dict(), all_edges_df=all_way_edges
            )
            _append_merged_edge_attrs(
                xml_edge=edge,
                sample_edge=first.to_dict(),
                edge_tags=edge_tags,
                edge_tag_aggs=edge_tag_aggs,
                all_edges_df=all_way_edges,
            )

    else:
        _create_way_for_each_edge(
            root=root,
            gdf_edges=gdf_edges,
            edge_attrs=edge_attrs,
            edge_tags=edge_tags,
        )

    return root


def _get_unique_nodes_ordered_from_way(df_way_edges: pd.DataFrame) -> list[Any]:
    """
    Recover original node order from edges associated with a single OSM way.

    Parameters
    ----------
    df_way_edges : pandas.DataFrame
        Dataframe containing columns 'u' and 'v' corresponding to
        origin/destination nodes.

    Returns
    -------
    unique_ordered_nodes : list
        An ordered list of unique node IDs. If the edges do not all connect
        (e.g. [(1, 2), (2,3), (10, 11), (11, 12), (12, 13)]), then this method
        will return only those nodes associated with the largest component of
        connected edges, even if subsequent connected chunks are contain more
        total nodes. This ensures a proper topological representation of nodes
        in the XML way records because if there are unconnected components,
        the sorting algorithm cannot recover their original order. We would
        not likely ever encounter this kind of disconnected structure of nodes
        within a given way, but it is not explicitly forbidden in the OSM XML
        design schema.
    """
    G = nx.MultiDiGraph()
    all_nodes = list(df_way_edges["u"].to_numpy()) + list(df_way_edges["v"].to_numpy())

    G.add_nodes_from(all_nodes)
    G.add_edges_from(df_way_edges[["u", "v"]].to_numpy())

    # copy nodes into new graph
    H = utils_graph.get_largest_component(G, strongly=False)
    unique_ordered_nodes = list(nx.topological_sort(H))
    num_unique_nodes = len(np.unique(all_nodes))

    if len(unique_ordered_nodes) < num_unique_nodes:
        utils.log(f"Recovered order for {len(unique_ordered_nodes)} of {num_unique_nodes} nodes")

    return unique_ordered_nodes