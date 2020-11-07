import logging
import datetime
from rucio.client import Client
from rucio.client.uploadclient import UploadClient
from rucio.common.exception import (
    InvalidRSEExpression,
    NoFilesUploaded,
    DataIdentifierNotFound,
)


logger = logging.getLogger(__name__)
ALLOWED_FILESIZES = {
    # motivation: one file every 6h = 100kbps avg. rate
    "270MB": 270000000,
}
LOADTEST_DATASET_FMT = "/LoadTestSource/{rse}/TEST#{filesize}"
LOADTEST_LFNDIR_FMT = "/store/test/loadtest/source/{rse}/"
LOADTEST_LFNBASE_FMT = "urandom.{filesize}.file{filenumber:04d}"


def generate_file(basename, nbytes):
    """Generates and writes a file

    Parameters
    ----------
        basename: str
        nbytes: int
    """
    with open("/dev/urandom", "rb") as fin:
        with open(basename, "wb") as fout:
            while nbytes > 0:
                b = min(1024 * 1024, nbytes)
                fout.write(fin.read(b))
                nbytes -= b


def prepare_upload_item(rse, filesize, filenumber):
    """Prepare a pseudorandom test file to upload to an RSE

    Parameters
    ----------
        rse: str
        filesize: str
        filenumber: int
    """
    dataset = LOADTEST_DATASET_FMT.format(rse=rse, filesize=filesize)
    dirname = LOADTEST_LFNDIR_FMT.format(rse=rse)
    basename = LOADTEST_LFNBASE_FMT.format(filesize=filesize, filenumber=filenumber)
    generate_file(basename, ALLOWED_FILESIZES[filesize])
    return {
        "path": basename,
        "rse": rse,
        "did_scope": "cms",
        "did_name": dirname + basename,
        "dataset_scope": "cms",
        "dataset_name": dataset,
        "register_after_upload": True,
    }


def ensure_rse_self_expression(client, rse):
    """Ensure one can use RSE name in expression

    RSE expressions with just the RSE name are resolved to a single RSE
    by having an attribute with the same name set to true for that RSE
    and not set on any other. This function ensures that fact.
    """
    try:
        matching = list(client.list_rses(rse_expression=rse))
        found = False
        for item in matching:
            if item["rse"] == rse:
                found = True
            else:
                logger.warning(
                    "Found extraneous RSE {item_rse} when checking RSE self-expression on {rse}".format(
                        item_rse=item["rse"], rse=rse
                    )
                )
                client.delete_rse_attribute(item["rse"], rse)
        if not found:
            logger.info("Repairing RSE self-expression for {rse}".format(rse=rse))
            client.add_rse_attribute(rse, rse, True)
    except InvalidRSEExpression as ex:
        if ex.message == u"RSE Expression resulted in an empty set.":
            logger.info("Repairing RSE self-expression for {rse}".format(rse=rse))
            client.add_rse_attribute(rse, rse, True)
        else:
            raise ex


def upload_source_data(client, uploader, rse, filesize, filenumber):
    item = prepare_upload_item(rse, filesize, filenumber)
    try:
        uploader.upload([item])
        return True
    except InvalidRSEExpression:
        logger.error("RSE {rse} is missing self-expression".format(rse=rse))
    except NoFilesUploaded:
        logger.error(
            "RSE {rse} already has a loadtest file matching {item}".format(
                rse=rse, item=item
            )
        )
    return False


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(name)s:%(levelname)s:%(message)s",
        level=logging.INFO,
    )
    account = "transfer_ops"
    activity = "Functional Test"
    filesize = "270MB"
    default_comment = "load:100kbps"
    source_rse_expression = "T2_US_Wisconsin_Test"
    dest_rse_expression = "T2_US_MIT_Test|T2_US_UCSD_Test"

    if filesize not in ALLOWED_FILESIZES:
        raise ValueError("File size {filesize} not allowed".format(filesize=filesize))
    client = Client(account=account)
    uploader = UploadClient(_client=client, logger=logger)

    source_rses = [item["rse"] for item in client.list_rses(source_rse_expression)]
    dest_rses = [item["rse"] for item in client.list_rses(dest_rse_expression)]

    for rse in set(source_rses) | set(dest_rses):
        ensure_rse_self_expression(client, rse)

    for source_rse in source_rses:
        dataset = LOADTEST_DATASET_FMT.format(rse=source_rse, filesize=filesize)
        try:
            source_files = list(client.list_files("cms", dataset))
        except DataIdentifierNotFound:
            logger.info(
                "RSE {source_rse} has no source files, creating one".format(
                    source_rse=source_rse
                )
            )
            upload_source_data(client, uploader, source_rse, filesize, 0)
            source_files = list(client.list_files("cms", dataset))

        dest_rules = client.list_replication_rules(
            {
                "scope": "cms",
                "name": dataset,
                "account": account,
                "activity": activity,
            }
        )
        dest_rules = {
            rule["rse_expression"]: rule
            for rule in dest_rules
            if rule["source_replica_expression"] == source_rse
        }

        for dest_rse in dest_rses:
            if dest_rse == source_rse:
                continue
            links = client.get_distance(source_rse, dest_rse)
            if len(links) == 0 and dest_rse in dest_rules:
                rule = dest_rules[dest_rse]
                logger.info(
                    "No link between {source_rse} and {dest_rse}, removing rule {rule_id}".format(
                        source_rse=source_rse, dest_rse=dest_rse, rule_id=rule["id"]
                    )
                )
                client.delete_replication_rule(rule["id"])
            elif len(links) == 0:
                continue
            elif len(links) > 1:
                logger.error(
                    "I have no idea what it means to have multiple links, carrying on..."
                )
            if dest_rse not in dest_rules:
                logger.info(
                    "New link between {source_rse} and {dest_rse}, creating a load test rule this cycle".format(
                        source_rse=source_rse, dest_rse=dest_rse
                    )
                )
                rule = {
                    "dids": [{"scope": "cms", "name": dataset}],
                    "copies": 1,
                    "rse_expression": dest_rse,
                    "source_replica_expression": source_rse,
                    "account": account,
                    "activity": activity,
                    "purge_replicas": True,
                    "ignore_availability": True,
                    "grouping": "DATASET",
                }
                logger.debug("Creating rule: %r" % rule)
                client.add_replication_rule(**rule)
                continue
            rule = dest_rules[dest_rse]
            if rule["state"] != "OK":
                logger.info(
                    "Existing link between {source_rse} and {dest_rse} with load test rule {rule_id} is in state {rule_state}, will skip load test replica update".format(
                        source_rse=source_rse,
                        dest_rse=dest_rse,
                        rule_id=rule["id"],
                        rule_state=rule["state"],
                    )
                )
                continue
            update_dt = (
                datetime.datetime.utcnow() - rule["updated_at"]
            ).total_seconds()
            logger.info(
                "Link between {source_rse} and {dest_rse} with load test rule {rule_id} last updated {update_dt}s ago, marking destination replicas unavailable".format(
                    source_rse=source_rse,
                    dest_rse=dest_rse,
                    rule_id=rule["id"],
                    update_dt=update_dt,
                )
            )
            replicas = [
                {"scope": file["scope"], "name": file["name"], "state": "U"}
                for file in source_files
            ]
            logger.debug(
                "Updating status for replicas: %r at RSE %s" % (replicas, dest_rse)
            )
            client.update_replicas_states(dest_rse, replicas)
            # judge-repairer will re-transfer after 2h
            # so max rate would be filesize * nfiles / (2*3600)
            # = 300 kbps for defaults
