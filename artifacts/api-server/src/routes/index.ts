import { Router, type IRouter } from "express";
import healthRouter from "./health";
import marketsRouter from "./markets";
import botRouter from "./bot";

const router: IRouter = Router();

router.use(healthRouter);
router.use(marketsRouter);
router.use(botRouter);

export default router;
