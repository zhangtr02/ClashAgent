from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene, QGraphicsView

from core.capture import ScreenFrame
from core.vision.models import Region


class PreviewCanvas(QGraphicsView):
    """Read-only live screenshot and recognition overlay."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#101824"))
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setMinimumSize(360, 220)
        self.setInteractive(False)
        self._pixmap_item = self.scene().addPixmap(QPixmap())
        self._regions = []
        self._frame = None

    def set_frame(self, frame: ScreenFrame):
        if self._frame is not None and self._frame.frame_id == frame.frame_id:
            return
        pixels = frame.image
        image = QImage(pixels.data, frame.width, frame.height, pixels.strides[0],
                       QImage.Format.Format_BGR888).copy()
        if image.isNull():
            raise ValueError("Qt 无法读取截图")
        self._frame = frame
        self._pixmap_item.setPixmap(QPixmap.fromImage(image))
        self.setSceneRect(0, 0, frame.width, frame.height)
        self.show_regions(())
        self.fit_image()

    def show_regions(self, regions: tuple[Region, ...]):
        while len(self._regions) < len(regions):
            item = QGraphicsRectItem()
            pen = QPen(QColor("#64d8b1"), 2)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setZValue(10)
            self.scene().addItem(item)
            self._regions.append(item)
        for index, item in enumerate(self._regions):
            item.setVisible(index < len(regions))
            if index < len(regions):
                r = regions[index]
                item.setRect(r.x, r.y, r.width, r.height)

    def fit_image(self):
        if self._frame:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_image()
