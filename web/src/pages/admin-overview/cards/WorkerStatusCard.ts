import { t } from "@lingui/macro";
import { customElement, html } from "lit-element";
import { AdminApi } from "@goauthentik/api";
import { DEFAULT_CONFIG } from "../../../api/Config";
import { AdminStatus, AdminStatusCard } from "./AdminStatusCard";

@customElement("ak-admin-status-card-workers")
export class WorkersStatusCard extends AdminStatusCard<number> {
    getPrimaryValue(): Promise<number> {
        return new AdminApi(DEFAULT_CONFIG).adminWorkersRetrieve().then((workers) => {
            return workers.count;
        });
    }

    getStatus(value: number): Promise<AdminStatus> {
        if (value < 1) {
            return Promise.resolve<AdminStatus>({
                icon: "fa fa-times-circle pf-m-danger",
                message: html`${t`No workers connected. Background tasks will not run.`}`,
            });
        } else {
            return Promise.resolve<AdminStatus>({
                icon: "fa fa-check-circle pf-m-success",
            });
        }
    }
}
